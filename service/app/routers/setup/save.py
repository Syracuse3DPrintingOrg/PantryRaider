"""POST /setup/save, the settings wizard's one save path, plus storage
categories."""

import asyncio
import re
from fastapi import Request
from pydantic import BaseModel

from ...config import (
    settings, DISPLAY_ROTATIONS, _DEFAULT_DISPLAY_ROTATION, DISPLAY_TYPES,
    FLOATING_NAV_POSITIONS, FLOATING_NAV_ORIENTATIONS, NAV_VISIBILITY, TIMER_CHIPS,
    CLOCK_FORMATS, _DEFAULT_CLOCK_FORMAT, SCREENSAVER_MODES, _DEFAULT_SCREENSAVER_MODE,
    STREAMDECK_KEY_STYLES, STREAMDECK_ICON_COLORS, DEPLOYMENT_MODES,
    SATELLITE_READONLY_FIELDS, KITCHEN_APPLIANCE_KEYS,
)
from ...dependencies import reset_providers
from ...hardware import is_raspberry_pi
from ...passwords import verify_secret
from ...navigation import (
    split_pane_pages, SPLIT_DEFAULT_PRIMARY, SPLIT_DEFAULT_SECONDARY,
)
from ...services.bridge import bridge_client
from ...storage_categories import _normalize_custom, storable
from ._common import (
    router, SetupPayload, _CLEAR, _DOCUMENT_PRINT_CHOICES, _HOST_BRIDGE, _SECRET_FIELDS,
    _carry_elevated_optin, _clean_custom_nav_tabs, _clean_nav_parents,
    _keep_unchanged_ha_flags, _merge_extra_keys, _merge_reolink_secrets,
    _merge_satellite_keys, _provision_touch_for_display, _push_display_idle,
    _push_update_channel, _write_kiosk_nav_flag,
)
from .cloud import _settle_pre_setup_cloud_link
from .remote_access import TunnelEnablePayload, tunnel_enable


# The one-shot provisioning kick started by a wizard save that beat the
# co-hosted Grocy to the finish line (see save_setup); kept so repeated saves
# never stack tasks.
_post_save_first_run = None


_KEY_FIELDS = ("api_key", "extra_api_keys", "extra_api_key_names")
_KEYS_MANAGED_MSG = "Keys are managed in Settings, Security."
_TUNNEL_NEEDS_PASSWORD_MSG = ("Turn off remote access first: a kitchen reachable from the "
                              "internet needs a password.")
_DECK_FIELDS = ("streamdeck_key_overrides", "streamdeck_ha_slots", "streamdeck_cameras",
                "streamdeck_ha_base_url", "streamdeck_ha_token")


def _review_ha_keys(data: dict, errors: dict, review_out: dict) -> None:
    """Run the posted Home Assistant keys through the key policy.

    First the owner's earlier "allow" choice is carried onto keys posted by
    an editor that does not know it (same id or slot position, same calls).
    Then a list with a key that is not valid is not saved at all (the stored
    keys stay exactly as they were) and the reply names it; keys that need
    the owner's permission are saved marked needs_review and listed in the
    reply. No other field of the save is affected.
    """
    from ...services import ha_allowlist
    stored_slots = settings.streamdeck_ha_slots or []
    if isinstance(data.get("streamdeck_key_overrides"), list):
        data["streamdeck_key_overrides"] = _carry_elevated_optin(
            data["streamdeck_key_overrides"], settings.streamdeck_key_overrides or [],
            data.get("streamdeck_ha_slots", stored_slots), stored_slots, match="id")
    if isinstance(data.get("streamdeck_ha_slots"), list):
        data["streamdeck_ha_slots"] = _carry_elevated_optin(
            data["streamdeck_ha_slots"], stored_slots,
            data["streamdeck_ha_slots"], stored_slots, match="position")
    slots = data.get("streamdeck_ha_slots", stored_slots)
    stored_by_field = {"streamdeck_key_overrides": settings.streamdeck_key_overrides or [],
                       "streamdeck_ha_slots": stored_slots}
    for field in ("streamdeck_key_overrides", "streamdeck_ha_slots"):
        if field not in data:
            continue
        match = "id" if field == "streamdeck_key_overrides" else "position"
        # A key that was already stored and is posted back unchanged never
        # blocks the save, even when the key policy calls it invalid: the
        # update flagged it on its own, and it may not even be on the page
        # doing the saving. Only a new or changed invalid key is refused.
        data[field], unchanged = _keep_unchanged_ha_flags(
            data[field], stored_by_field[field], slots, stored_slots, match=match)
        entries, errs, review = ha_allowlist.review_entries(data[field], slots)
        errs = [e for e in errs
                if (str(e.get("id") or ""), str(e.get("slot"))) not in unchanged]
        if errs:
            data.pop(field, None)
            errors[field] = errs
            continue
        data[field] = entries
        if review:
            review_out[field] = review


def _blank_ha_camera_token_urls(cams: list) -> list:
    """Blank a token-carrying address on a Home Assistant camera.

    Cameras added from Home Assistant before 0.21 saved addresses with the
    long-lived token in them. The camera works through its entity, so the
    address is not needed and the token is not kept."""
    from ...services.deck_config import strip_ha_camera_token
    out = []
    for c in cams:
        c, _blanked = strip_ha_camera_token(c)
        if isinstance(c, dict) and str(c.get("ha_entity") or "").strip():
            c = dict(c)
            for field in ("snapshot_url", "stream_url"):
                # A discovery preview address is admin-only and not a feed.
                if str(c.get(field) or "").startswith("ui/camera/preview"):
                    c[field] = ""
        out.append(c)
    return out


@router.post("/save")
async def save_setup(request: Request, payload: SetupPayload):
    # Whether this save is the one that completes first-time setup is decided
    # by comparing the configured state before and after; capture "before"
    # first, ahead of anything that touches settings (FoodAssistant-6v9q).
    was_configured = settings.is_configured()
    data = payload.model_dump(exclude_unset=True)
    # A request flag, not a setting: acted on once the save has landed (below).
    want_remote_access = bool(data.pop("forager_remote_access", None))
    # Changing or removing an existing login password requires the current one,
    # so an unattended or hijacked settings session cannot lock out the owner
    # (FoodAssistant-f403). Setting the first password (none stored yet) needs
    # no current password. A blank auth_password means "keep", so it is exempt.
    if payload.auth_password and settings.auth_password \
            and not verify_secret(payload.current_password, settings.auth_password):
        return {"ok": False,
                "error": "Enter your current password to change or remove it."}
    # On a satellite the backend config is owned by the main server and pulled on
    # each sync, and the Grocy and Mealie keys stay on the server altogether.
    # Drop any user edit to those fields here so it is not saved and then
    # silently overwritten or ignored (the panes show them read-only,
    # but this guards a stray or scripted POST). The sync path persists pulled
    # values through settings.save() directly, which this does not touch.
    if settings.is_satellite():
        for f in SATELLITE_READONLY_FIELDS:
            data.pop(f, None)
    for f in _SECRET_FIELDS:
        if data.get(f) == "":
            data.pop(f, None)        # blank = keep existing value
        elif data.get(f) == _CLEAR:
            data[f] = ""             # explicit clear
    # Tri-state AI shelf-life toggle: only an explicit true/false persists.
    # A null (or absent) value keeps the stored choice, including the
    # never-chosen state that follows whether AI is configured.
    if "llm_expiry_enabled" in data and data["llm_expiry_enabled"] is None:
        data.pop("llm_expiry_enabled", None)
    data["ai_extra_keys"] = _merge_extra_keys(data.get("ai_extra_keys"))
    if data["ai_extra_keys"] is None:
        data.pop("ai_extra_keys", None)   # absent = keep stored extras
    merged_sat = _merge_satellite_keys(data.get("extra_api_keys"))
    if merged_sat is None:
        data.pop("extra_api_keys", None)  # absent = keep stored extras
        data.pop("extra_api_key_names", None)
    else:
        data["extra_api_keys"], data["extra_api_key_names"] = merged_sat
    # Replies for parts of this save that were not applied. Nothing in here
    # ever stops the rest of the save from landing.
    errors: dict = {}
    review_out: dict = {}
    # Once set up, keys are created and removed in Settings, Security (with
    # names, scopes and a reveal-once flow). A blank api_key was already
    # dropped above as "keep"; anything still here comes from an older page
    # or a script. Before setup completes a first key can still be set here.
    if was_configured and any(k in data for k in _KEY_FIELDS):
        for k in _KEY_FIELDS:
            data.pop(k, None)
        errors["api_keys"] = _KEYS_MANAGED_MSG
    # A kitchen reachable from the internet through the tunnel needs its
    # password: turning it off, or clearing it, waits until remote access is off.
    if settings.tunnel_enabled and (data.get("auth_required") is False
                                    or data.get("auth_password") == ""):
        data.pop("auth_required", None)
        data.pop("auth_password", None)
        errors["auth"] = _TUNNEL_NEEDS_PASSWORD_MSG
    if data.get("display_rotation") not in DISPLAY_ROTATIONS:
        data["display_rotation"] = _DEFAULT_DISPLAY_ROTATION
    # Deck rotation: only a supported angle persists (FoodAssistant-kl5n).
    if "streamdeck_rotation" in data and data["streamdeck_rotation"] not in DISPLAY_ROTATIONS:
        data["streamdeck_rotation"] = _DEFAULT_DISPLAY_ROTATION
    # 12/24-hour clock reading: only the known values persist.
    if "clock_format" in data and data["clock_format"] not in CLOCK_FORMATS:
        data["clock_format"] = _DEFAULT_CLOCK_FORMAT
    # Shopping-list backend: only the known values persist ("" = automatic).
    if ("shopping_backend" in data
            and data["shopping_backend"] not in ("", "grocy", "mealie")):
        data["shopping_backend"] = ""
    # Bandit Cub content settings: only known views and blocks persist, and
    # the second-counts stay in a sane range. None means the field was not
    # submitted, so it is dropped and the stored value kept.
    from ...services.cub import CUB_ROTATION_BLOCKS, CUB_VIEWS
    for f in ("cub_default_view", "cub_timers_take_over", "cub_probes_take_over",
              "cub_alerts_take_over",
              "cub_rotation", "cub_rotate_seconds", "cub_poll_seconds"):
        if f in data and data[f] is None:
            data.pop(f, None)
    if "cub_default_view" in data and data["cub_default_view"] not in CUB_VIEWS:
        data["cub_default_view"] = "expiring"
    if "cub_rotation" in data:
        data["cub_rotation"] = [b for b in data["cub_rotation"]
                                if b in CUB_ROTATION_BLOCKS]
    if "cub_rotate_seconds" in data:
        try:
            data["cub_rotate_seconds"] = max(3, min(120, int(data["cub_rotate_seconds"])))
        except (TypeError, ValueError):
            data["cub_rotate_seconds"] = 12
    if "cub_poll_seconds" in data:
        try:
            data["cub_poll_seconds"] = max(5, min(300, int(data["cub_poll_seconds"])))
        except (TypeError, ValueError):
            data["cub_poll_seconds"] = 15
    # The device toggles that ride the same "None means the field was not
    # submitted" contract: Cub firmware auto-update, the Cub Bluetooth beacon,
    # and the satellite sensor relay. Dropping a null keeps the stored value
    # instead of writing None over a boolean setting.
    for f in ("cub_auto_update", "cub_ble_advertise", "relay_gadgets_upstream"):
        if f in data and data[f] is None:
            data.pop(f, None)
    # Split view (FoodAssistant-dqpq): a pane page must be a known nav page
    # key; an unknown one falls back to that pane's default so a pane can
    # never point at a dead page. The booleans ride the same "None means the
    # field was not posted" contract as the other display toggles.
    for f in ("kiosk_split_enabled", "kiosk_split_lock_primary"):
        if f in data and data[f] is None:
            data.pop(f, None)
    _split_keys = {p["key"] for p in split_pane_pages()}
    for f, _default in (("kiosk_split_primary", SPLIT_DEFAULT_PRIMARY),
                        ("kiosk_split_secondary", SPLIT_DEFAULT_SECONDARY)):
        if f in data:
            if data[f] is None:
                data.pop(f, None)
            elif data[f] not in _split_keys:
                data[f] = _default
    # Screensaver pill size: only the known values persist.
    if ("screensaver_pill_scale" in data
            and data["screensaver_pill_scale"] not in ("normal", "large", "xlarge")):
        data["screensaver_pill_scale"] = "normal"
    # Ken Burns pan/zoom speed: only the known values persist.
    if ("screensaver_ken_burns_speed" in data
            and data["screensaver_ken_burns_speed"] not in ("slow", "normal", "fast")):
        data["screensaver_ken_burns_speed"] = "normal"
    # Screensaver style: an unknown mode falls back to the bouncing logo so a
    # stray value never leaves the panel on a mode the browser cannot draw.
    if ("screensaver_mode" in data
            and data["screensaver_mode"] not in SCREENSAVER_MODES):
        data["screensaver_mode"] = _DEFAULT_SCREENSAVER_MODE
    # Photo source: only the known source names persist (FoodAssistant-af1l).
    if "photo_source" in data:
        from ...services.photo_source import (normalize_photo_source,
                                             invalidate_immich_cache)
        data["photo_source"] = normalize_photo_source(data["photo_source"])
        # A source change must not serve a stale Immich album listing.
        invalidate_immich_cache()
    # Seconds per slideshow photo: keep it in a sane range.
    if "screensaver_photo_seconds" in data:
        try:
            data["screensaver_photo_seconds"] = max(2, min(120, int(data["screensaver_photo_seconds"])))
        except (TypeError, ValueError):
            data["screensaver_photo_seconds"] = 25
    # AI token budget: non-negative integer (0 = no budget).
    if "ai_token_budget" in data:
        try:
            data["ai_token_budget"] = max(0, int(data["ai_token_budget"]))
        except (TypeError, ValueError):
            data.pop("ai_token_budget", None)
    # On-screen Start Page: only 6/15/32 keys are valid (Stream Deck sizes).
    if "start_page_keys" in data and data["start_page_keys"] not in (6, 15, 32):
        data["start_page_keys"] = 15
    # Start Page mode: only the two known modes persist. None means the field was
    # not submitted, so drop it and keep the stored mode.
    if "start_page_mode" in data:
        if data["start_page_mode"] is None:
            data.pop("start_page_mode", None)
        elif data["start_page_mode"] not in ("glance", "custom"):
            data["start_page_mode"] = "glance"
    if data.get("start_page_layout") is None:
        data.pop("start_page_layout", None)  # absent = keep stored layout
    # Merge custom-key definitions built on the Start Page into the shared deck
    # store (Pantry Raider). Custom keys are shared both ways without the Start
    # Page needing the deck's slots:
    #   * update an existing key by id, keeping its Stream Deck slot;
    #   * add a new key unplaced (slot -1);
    #   * drop a key ONLY when the editor that opened it (start_loaded_ids) no
    #     longer lists it, so a key added on the deck since this page loaded is
    #     never clobbered by a stale Start Page save.
    defs = data.pop("start_custom_defs", None)
    loaded_ids = set(data.pop("start_loaded_ids", None) or [])
    if isinstance(defs, list):
        existing = settings.streamdeck_key_overrides or []
        defs_by_id = {d["id"]: d for d in defs
                      if isinstance(d, dict) and d.get("id")}
        result, kept = [], set()
        for o in existing:
            if not isinstance(o, dict) or not o.get("id"):
                continue
            oid = o["id"]
            if oid in defs_by_id:
                entry = dict(defs_by_id[oid]); entry["slot"] = o.get("slot", -1)
                result.append(entry)
            elif oid in loaded_ids:
                continue  # the user removed it on the Start Page
            else:
                result.append(o)  # untouched by this editor; keep as-is
            kept.add(oid)
        for d in defs:
            if isinstance(d, dict) and d.get("id") and d["id"] not in kept:
                entry = dict(d); entry["slot"] = -1; result.append(entry)
        data["streamdeck_key_overrides"] = result
    # Home Assistant keys go through the key policy before they are saved.
    _review_ha_keys(data, errors, review_out)
    # Background image (FoodAssistant-e2t6): clamp opacity to 0-100 and only
    # accept an http(s) or the internal serve route as the image URL, so a saved
    # value can never inject a javascript:/data: URL into the CSS background.
    if "background_opacity" in data:
        try:
            data["background_opacity"] = max(0, min(100, int(data["background_opacity"])))
        except (TypeError, ValueError):
            data.pop("background_opacity", None)
    if "background_image_url" in data:
        u = (data["background_image_url"] or "").strip()
        if u and not (u.startswith("http://") or u.startswith("https://")
                      or u.startswith("setup/background/image")):
            data.pop("background_image_url", None)
        else:
            data["background_image_url"] = u
    # Drop an unknown display type rather than persisting a broken value; an
    # absent value leaves the stored choice untouched.
    if "display_type" in data and data["display_type"] not in DISPLAY_TYPES:
        data.pop("display_type", None)
    # Drop an unknown floating-nav position (empty/invalid keeps the stored one).
    if "floating_nav_position" in data and data["floating_nav_position"] not in FLOATING_NAV_POSITIONS:
        data.pop("floating_nav_position", None)
    # Same for an unknown floating-nav orientation.
    if "floating_nav_orientation" in data and data["floating_nav_orientation"] not in FLOATING_NAV_ORIENTATIONS:
        data.pop("floating_nav_orientation", None)
    # Drop an unknown nav-visibility value (empty/invalid keeps the stored one).
    if "nav_visibility" in data and data["nav_visibility"] not in NAV_VISIBILITY:
        data.pop("nav_visibility", None)
    # Drop an unknown timer-chips value (empty/invalid keeps the stored one).
    if "timer_chips" in data and data["timer_chips"] not in TIMER_CHIPS:
        data.pop("timer_chips", None)
    # Drop an unknown QR address mode (empty/invalid keeps the stored one).
    if "qr_url_mode" in data and data["qr_url_mode"] not in ("auto", "public"):
        data.pop("qr_url_mode", None)
    # The QR public URL must be a plain http(s) address (or empty to clear it).
    if "qr_public_url" in data:
        u = (data["qr_public_url"] or "").strip()
        if u and not (u.startswith("http://") or u.startswith("https://")):
            data.pop("qr_public_url", None)
        else:
            data["qr_public_url"] = u.rstrip("/")
    # Timezone: "" (auto/system) or a valid IANA name; drop anything else so a
    # typo never breaks timestamp rendering.
    if "timezone" in data and data["timezone"]:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(data["timezone"])
        except Exception:
            data.pop("timezone", None)
    # Scheduled reboot time: "" (off) or 24h HH:MM.
    if "scheduled_reboot_time" in data and data["scheduled_reboot_time"]:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(data["scheduled_reboot_time"])):
            data.pop("scheduled_reboot_time", None)
    # Reboot frequency: "" (legacy, a set time means nightly), off, nightly,
    # or weekly; the day is 0=Sunday .. 6=Saturday. Anything else keeps the
    # stored value (FoodAssistant-8x4u).
    if "scheduled_reboot_frequency" in data and (
            data["scheduled_reboot_frequency"] not in ("", "off", "nightly", "weekly")):
        data.pop("scheduled_reboot_frequency", None)
    if "scheduled_reboot_day" in data:
        try:
            day = int(data["scheduled_reboot_day"])
        except (TypeError, ValueError):
            day = -1
        if 0 <= day <= 6:
            data["scheduled_reboot_day"] = day
        else:
            data.pop("scheduled_reboot_day", None)
    # Drop an unknown Stream Deck key style / icon colour (keeps the stored one).
    if "streamdeck_key_style" in data and data["streamdeck_key_style"] not in STREAMDECK_KEY_STYLES:
        data.pop("streamdeck_key_style", None)
    if "streamdeck_icon_color" in data and data["streamdeck_icon_color"] not in STREAMDECK_ICON_COLORS:
        data.pop("streamdeck_icon_color", None)
    # Keep only known appliance ids, de-duplicated in catalog order, so a stale
    # or hand-crafted id never reaches the AI prompt. An empty list is preserved
    # (the user owns none); the field absent leaves the stored choice untouched.
    if "kitchen_appliances" in data:
        submitted = data["kitchen_appliances"]
        if isinstance(submitted, list):
            chosen = {str(x) for x in submitted}
            data["kitchen_appliances"] = [k for k in KITCHEN_APPLIANCE_KEYS if k in chosen]
        else:
            data.pop("kitchen_appliances", None)
    # Plug-in accessories (FoodAssistant-t0vp5): every entry goes through the
    # same cleaning the /gadgets/stemma endpoints apply (a bus-plus-address
    # id, a supported kind, the per-kind options filled in), junk is dropped,
    # a repeated id keeps its first entry, and anything that is not a list
    # keeps the stored registry.
    if "stemma_devices" in data:
        submitted = data["stemma_devices"]
        if isinstance(submitted, list):
            from ...services import stemma as _stemma
            cleaned: list[dict] = []
            for raw in submitted:
                dev = _stemma.normalize_device(raw)
                if dev and all(dev["id"] != d["id"] for d in cleaned):
                    cleaned.append(dev)
            data["stemma_devices"] = cleaned
        else:
            data.pop("stemma_devices", None)
    # Custom nav tabs (FoodAssistant-9gdz): normalize to {id,label,icon,url,
    # parent} and drop invalid entries so a malformed POST never persists. An
    # absent field leaves the stored tabs alone; an empty list clears them.
    # When any tab's address is not allowed (javascript: and the like), none
    # of the posted tabs are saved: the stored ones stay as they were, every
    # other field in this save still lands, and the reply names the tabs to fix
    # (errors.custom_nav_tabs) so the editor can mark them.
    nav_tab_errors: list[dict] = []
    if "custom_nav_tabs" in data:
        if data["custom_nav_tabs"] is None:
            data.pop("custom_nav_tabs", None)
        else:
            clean_tabs, nav_tab_errors = _clean_custom_nav_tabs(data["custom_nav_tabs"])
            if nav_tab_errors:
                data.pop("custom_nav_tabs", None)
            else:
                data["custom_nav_tabs"] = clean_tabs
    # Built-in nesting map: keep only string->string pairs for known tab keys.
    if "nav_parents" in data:
        if data["nav_parents"] is None:
            data.pop("nav_parents", None)
        else:
            data["nav_parents"] = _clean_nav_parents(data["nav_parents"])
    # Drop an unknown deployment mode rather than persisting a broken value;
    # an empty/absent mode leaves the existing choice untouched.
    if data.get("deployment_mode") and data["deployment_mode"] not in DEPLOYMENT_MODES:
        data.pop("deployment_mode", None)
    # Same for an unknown wake-on-motion mode: keep the stored value.
    if "wake_on_motion" in data and data["wake_on_motion"] not in ("auto", "on", "off"):
        data.pop("wake_on_motion", None)
    # And an unknown wake-on-presence mode.
    if "wake_on_presence" in data and data["wake_on_presence"] not in ("auto", "on", "off"):
        data.pop("wake_on_presence", None)
    # And the update channel: only "stable" (releases) or "main" (every change).
    if "update_channel" in data and data["update_channel"] not in ("stable", "main"):
        data.pop("update_channel", None)
    # Same rule as the tunnel router: only a mode this install runs is stored.
    if "tunnel_mode" in data and data["tunnel_mode"] not in ("", "cloudflare", "forager"):
        data.pop("tunnel_mode", None)
    if data.get("label_shape") is not None:
        from ...services.label_render import normalize_shape
        data["label_shape"] = normalize_shape(data["label_shape"])
    # Document printer options: an unknown choice falls back to the default.
    if any(data.get(k) is not None for k in _DOCUMENT_PRINT_CHOICES):
        from ...services import print_document
        for k, (choices, default) in _DOCUMENT_PRINT_CHOICES.items():
            if data.get(k) is not None:
                v = str(data[k]).strip().lower()
                data[k] = v if v in getattr(print_document, choices) else default
    if data.get("remote_server_url"):
        data["remote_server_url"] = data["remote_server_url"].rstrip("/")
    # The backup remote later becomes a positional rclone argument, so only a
    # safe remote:path (or absolute path) shape is ever stored; anything else,
    # in particular a value starting with "-", is dropped so it can never
    # smuggle a flag into the rclone command (see services/backup_remote.py).
    if "rclone_remote" in data:
        from ...services.backup_remote import valid_remote
        v = (data["rclone_remote"] or "").strip()
        if v and not valid_remote(v):
            data.pop("rclone_remote", None)
        else:
            data["rclone_remote"] = v   # "" clears the remote
    # A Reolink camera's login lives on the server and is never rendered into the
    # page, so a plain "Save cameras" (which rebuilds the list from the visible
    # rows) posts the entry without its password. Restore the stored password by
    # matching the camera's identity, the same "blank keeps the secret" rule the
    # HA token uses, so re-saving the list does not wipe the credentials.
    if "streamdeck_cameras" in data and isinstance(data["streamdeck_cameras"], list):
        data["streamdeck_cameras"] = _blank_ha_camera_token_urls(
            _merge_reolink_secrets(data["streamdeck_cameras"]))
    # A pasted Home Assistant token is a manual one: the account checks run
    # again for it. Clearing the token clears what was known about it.
    ha_token_pasted = False
    if "streamdeck_ha_token" in data:
        if data["streamdeck_ha_token"]:
            data["ha_token_kind"] = "manual"
            data["ha_token_admin"] = None
            data["ha_token_expires"] = ""
            ha_token_pasted = True
        else:
            data["ha_token_kind"] = ""
            data["ha_token_admin"] = None
            data["ha_token_expires"] = ""
    old_passwords = (settings.auth_password, settings.viewer_password)
    settings.save(data)
    if (settings.auth_password, settings.viewer_password) != old_passwords:
        # A new or removed password signs out every other browser; the one
        # that made the change stays signed in.
        from ...services import auth_state
        auth_state.sign_out_others(request.session)
        if not was_configured and not old_passwords[0] and settings.auth_password:
            # The first password, set from the wizard before setup is done.
            # The wizard needs a sign-in from now on, and this browser was
            # the one allowed to set it, so it stays in the wizard.
            request.session["authed"] = True
            request.session["role"] = "admin"
    if ha_token_pasted:
        try:
            from ...services import ha_client
            ha_client.invalidate_checks()
        except Exception:  # noqa: BLE001 - best effort
            pass
    # The deck mirrors the Home Assistant keys, connection and cameras; bring
    # it up to date now (a no-op off a Pi or without a deck).
    if not settings.is_satellite() and any(k in data for k in _DECK_FIELDS):
        try:
            from ...services import deck_config
            await deck_config.apush_to_deck()
        except Exception:  # noqa: BLE001 - best effort
            pass
    reset_providers()   # apply new provider/model/key without a restart
    from ...services.recipe_source import reset_mealie_cache
    from ...services.recipe_match import reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    # The save that flips the install to configured IS setup completing, so the
    # server raises the kiosk hand-off flag right here (FoodAssistant-6v9q).
    # The wizard used to POST setup/kiosk/navigate/request afterwards, but a
    # completing save is also the moment auth goes live, so that follow-up
    # request from the (remote, sessionless) browser always answered 401 and
    # the attached display sat on the wizard splash until something external
    # reloaded it. Writing the flag inside the privileged act itself needs no
    # new auth surface and cannot be missed. Only the transition writes it: a
    # routine settings save on a configured install must never yank the kiosk
    # display back to the home screen.
    if not was_configured and settings.is_configured():
        _write_kiosk_nav_flag()
        _settle_pre_setup_cloud_link(request.session)
    # The wizard's Remote Access step only records the choice; the tunnel
    # comes up here, once the install is configured and so behind its login,
    # rather than through a pre-setup call to /setup/tunnel/enable
    # (FoodAssistant-mzsci). It runs after the pre-setup Forager link is
    # settled, so a link a stranger made before setup is already gone and the
    # enable refuses. tunnel_enable keeps all of its own gates (linked,
    # password, a second factor, a host that can run the tunnel), and a
    # refusal never undoes the rest of the save.
    remote_access = None
    if want_remote_access and settings.is_configured():
        remote_access = await tunnel_enable(TunnelEnablePayload())
        if remote_access.get("ok"):
            # Same bookkeeping the settings pane does after its own enable.
            settings.save({"tunnel_mode": "forager"})
    # Finishing the wizard while the co-hosted inventory is still coming up
    # (a slow first boot) must not leave the install waiting on the startup
    # provisioner's slow cadence, or worse, on one that already gave up. Kick
    # a fresh zero-touch run that polls briskly; it exits on its own the
    # moment the key exists or Grocy turns out to be someone's (0m61).
    global _post_save_first_run
    if (not settings.is_satellite() and settings.grocy_base_url
            and not settings.grocy_api_key
            and (_post_save_first_run is None or _post_save_first_run.done())):
        from ...services.first_run import startup_first_run
        _post_save_first_run = asyncio.create_task(
            startup_first_run(attempts=60, delay=10.0, initial_delay=0))
    # Mirror the kiosk display idle timeout to the host bridge, which owns the
    # blanking loop (FoodAssistant-otiy). Best-effort and Pi-only.
    if "display_idle_timeout" in data or "wake_on_motion" in data or "wake_on_presence" in data:
        await _push_display_idle()
    # Auto-provision the touch overlay when the display type is (re)chosen, so an
    # ADS7846 SPI panel gets SPI + its overlay written without a separate button
    # press (FoodAssistant-vbfp). Best-effort and Pi-only; a reboot loads it.
    resp = {"ok": True}
    if remote_access is not None:
        resp["remote_access"] = remote_access
    if nav_tab_errors:
        errors["custom_nav_tabs"] = nav_tab_errors
    if errors:
        resp["errors"] = errors
    if review_out:
        resp["review"] = review_out
    if data.get("display_type") and is_raspberry_pi():
        provisioned = await _provision_touch_for_display(data["display_type"])
        if provisioned and provisioned.get("needs_reboot"):
            resp["touch_needs_reboot"] = True
    # Mirror the timezone and nightly-reboot schedule to the host (Pi appliance),
    # so the system clock and the reboot timer match the saved settings. Best
    # effort: a missing/old bridge just leaves the host as-is.
    if settings.is_pi_appliance():
        # Mirror the update channel to the host as well: the OTA helper runs on
        # the host and reads /etc/foodassistant/update-channel, which the app
        # container cannot write itself, so the bridge lands it there (wkwx).
        # Best effort; the update trigger re-pushes it so a stale bridge only
        # delays the switch by one update.
        if "update_channel" in data:
            await _push_update_channel(settings.update_channel)
        if data.get("timezone"):
            try:
                async with bridge_client(timeout=15.0) as c:
                    await c.post(f"{_HOST_BRIDGE}/system/timezone",
                                 json={"tz": data["timezone"]})
            except Exception:
                pass
        if any(k in data for k in ("scheduled_reboot_time",
                                   "scheduled_reboot_frequency",
                                   "scheduled_reboot_day")):
            # Push the effective schedule from the merged settings, so a save
            # of just the frequency (or just the day) still lands the whole
            # picture on the host. An empty frequency keeps the legacy rule:
            # a set time means nightly (FoodAssistant-8x4u).
            freq = settings.scheduled_reboot_frequency or (
                "nightly" if settings.scheduled_reboot_time else "off")
            try:
                async with bridge_client(timeout=15.0) as c:
                    await c.post(f"{_HOST_BRIDGE}/system/scheduled-reboot",
                                 json={"time": "" if freq == "off" else settings.scheduled_reboot_time,
                                       "frequency": freq,
                                       "day": settings.scheduled_reboot_day})
            except Exception:
                pass
    return resp


class StorageCategoriesPayload(BaseModel):
    categories: list[dict] = []


@router.post("/storage-categories")
async def save_storage_categories(payload: StorageCategoriesPayload):
    """Replace the set of user-defined storage categories.

    Entries are normalized/validated (blank, keyless, or built-in-colliding
    rows are dropped) before saving, so the inventory dashboard never sees a
    malformed category.
    """
    clean = [storable(c) for c in _normalize_custom(payload.categories)]
    settings.save({"custom_storage_categories": clean})
    return {"ok": True, "categories": clean}
