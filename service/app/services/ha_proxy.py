"""What the Home Assistant part of the fleet proxy (/api/proxy/ha) may carry.

The Stream Deck on this device, and satellites whose main server holds the
Home Assistant connection, reach Home Assistant through this app instead of
holding a Home Assistant token themselves. The hop carries exactly three
shapes, and only for what the owner has set up:

- GET api/states/<entity>: the state of an entity some key, camera,
  thermometer or hygrometer uses (ha_allowlist.readable_entities).
- GET api/camera_proxy/<camera entity>: a picture from a configured camera
  (ha_allowlist.camera_entities).
- POST api/services/<domain>/<service> with a body whose only key is
  entity_id: a call every one of whose (entity, service) pairs a key makes
  (ha_allowlist.configured_calls) and the key policy permits
  (ha_policy.permitted, enforced exactly as the rest of the app enforces it,
  ha_allowlist.policy_enforced). One pair that fails refuses the whole call.

parse() and authorize() are pure: the router does the talking, through
services/ha_client.py, and the refusal never says which part failed, never
echoes the body and never carries a token.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from . import ha_allowlist, ha_policy

NOT_ALLOWED_MSG = "That Home Assistant action is not set up here."
CODE_NOT_ALLOWED = "ha_not_allowed"
NOT_CONNECTED_MSG = "Home Assistant is not connected on the main server."
NOT_FOUND_MSG = "That Home Assistant entity was not found."
NO_PICTURE_MSG = "Home Assistant did not send a picture for that camera."
UNAUTHORIZED_MSG = "Unauthorized"

# State attributes that carry Home Assistant's own short-lived access tokens
# (a camera's access_token, and the ?token= in a camera or media player's
# entity_picture). Nothing that reads a state through the hop uses them, and
# with them a device could open the camera's live stream from Home Assistant
# directly, so they are left out.
_TOKEN_ATTRIBUTES = ("access_token",)
_TOKEN_URL_ATTRIBUTES = ("entity_picture",)

KIND_STATE = "state"
KIND_CAMERA = "camera"
KIND_SERVICE = "service"

# A service-call body is a tiny JSON object; anything larger is not one the
# deck or a satellite sends.
MAX_BODY_BYTES = 16 * 1024

# Keys that act for a satellite on this hop, the same ones the Grocy and
# Mealie hops accept.
SATELLITE_SCOPES = frozenset({"satellite", "legacy", "admin"})

_PART_RE = re.compile(r"^[a-z0-9_]+$")


class HaRefused(Exception):
    """A request the hop answers itself with ``status`` and ``detail``."""

    status = 403
    detail = NOT_ALLOWED_MSG
    code = CODE_NOT_ALLOWED

    def body(self) -> dict:
        out = {"detail": self.detail}
        if self.code:
            out["code"] = self.code
        return out


class HaNotAllowed(HaRefused):
    """Not a shape the hop carries, or not set up and permitted here (403)."""


class HaUnauthorized(HaRefused):
    """No credential this hop accepts (401)."""

    status = 401
    detail = UNAUTHORIZED_MSG
    code = ""


@dataclass(frozen=True)
class Call:
    kind: str
    entity_ids: tuple
    domain: str = ""
    service: str = ""

    @property
    def service_id(self) -> str:
        return f"{self.domain}.{self.service}" if self.domain else ""

    @property
    def entity_string(self) -> str:
        """The normalized ids joined with ',', the form Home Assistant gets."""
        return ",".join(self.entity_ids)

    def body_bytes(self) -> bytes:
        """The forwarded service-call body; b'' for the two reads."""
        if self.kind != KIND_SERVICE:
            return b""
        return json.dumps({"entity_id": self.entity_string}).encode("utf-8")

    def path(self) -> str:
        """The Home Assistant API path, every part quoted on its own."""
        if self.kind == KIND_STATE:
            return "api/states/" + quote(self.entity_ids[0], safe="")
        if self.kind == KIND_CAMERA:
            return "api/camera_proxy/" + quote(self.entity_ids[0], safe="")
        return (f"api/services/{quote(self.domain, safe='')}/"
                f"{quote(self.service, safe='')}")


@dataclass(frozen=True)
class Authorized:
    """Who asked (the ha_policy source) and, for a service call, the entry
    whose allow_elevated governs each entity."""
    source: str
    entries: dict = field(default_factory=dict)


def _one_entity(raw: str) -> str:
    entity = str(raw or "").strip().lower()
    if not ha_policy.ID_RE.fullmatch(entity):
        raise HaNotAllowed()
    return entity


def _entities_from_body(body_bytes) -> tuple:
    if not body_bytes or len(body_bytes) > MAX_BODY_BYTES:
        raise HaNotAllowed()
    try:
        data = json.loads(body_bytes)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise HaNotAllowed() from None
    if not isinstance(data, dict) or set(data) != {"entity_id"}:
        raise HaNotAllowed()
    value = data["entity_id"]
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) for item in value):
            raise HaNotAllowed()
    elif not isinstance(value, str):
        raise HaNotAllowed()
    entity, _svc = ha_policy.normalize(value, "")
    items = entity.split(",")
    if (not entity or len(items) > ha_policy.MAX_ENTITIES
            or any(not ha_policy.ID_RE.fullmatch(item) for item in items)):
        # 'all', 'none', an empty item or too many ids: never forwarded.
        raise HaNotAllowed()
    return tuple(items)


def parse(method, path_segments, body_bytes=b"") -> Call:
    """The Call a request makes, or HaNotAllowed. ``path_segments`` is what
    proxy_policy.validate_path returned. Pure."""
    m = str(method or "").upper()
    segs = list(path_segments or [])
    if len(segs) < 3 or segs[0] != "api":
        raise HaNotAllowed()
    if m == "GET" and len(segs) == 3 and segs[1] == "states":
        return Call(KIND_STATE, (_one_entity(segs[2]),))
    if m == "GET" and len(segs) == 3 and segs[1] == "camera_proxy":
        entity = _one_entity(segs[2])
        if not entity.startswith("camera."):
            raise HaNotAllowed()
        return Call(KIND_CAMERA, (entity,))
    if m == "POST" and len(segs) == 4 and segs[1] == "services":
        domain = segs[2].strip().lower()
        service = segs[3].strip().lower()
        if not (_PART_RE.fullmatch(domain) and _PART_RE.fullmatch(service)):
            raise HaNotAllowed()
        return Call(KIND_SERVICE, _entities_from_body(body_bytes), domain, service)
    raise HaNotAllowed()


def public_state(state: dict) -> dict:
    """A copy of an entity state without the attributes that carry Home
    Assistant access tokens. The cached original is never changed. Pure."""
    if not isinstance(state, dict):
        return state
    attrs = state.get("attributes")
    if not isinstance(attrs, dict):
        return dict(state)
    clean = {}
    for name, value in attrs.items():
        if name in _TOKEN_ATTRIBUTES:
            continue
        if (name in _TOKEN_URL_ATTRIBUTES and isinstance(value, str)
                and "token=" in value.lower()):
            continue
        clean[name] = value
    out = dict(state)
    out["attributes"] = clean
    return out


def caller_source(principal) -> str | None:
    """'satellite' for a satellite, legacy or admin key, 'deck' for this
    device's Stream Deck (its agent key, or the old trust for programs on
    this device while it lasts), None for anyone else."""
    kind = getattr(principal, "kind", "anonymous")
    scope = getattr(principal, "scope", None)
    if kind == "key" and scope in SATELLITE_SCOPES:
        return "satellite"
    if kind == "agent" and scope == "deck":
        return "deck"
    if kind == "legacy_loopback" and getattr(principal, "origin", "") == "plain_loopback":
        return "deck"
    return None


def authorize(call: Call, principal, settings, profiles) -> Authorized:
    """Authorized(source, entries), or HaUnauthorized / HaNotAllowed.

    Nobody signed in with a key gets 401. A key or agent this hop does not
    serve, or a call the owner has not set up, gets the same 403 whatever the
    reason, so the answer never says what is configured. Pure apart from
    reading settings and the profiles passed in."""
    source = caller_source(principal)
    if source is None:
        if getattr(principal, "kind", "anonymous") in ("key", "agent"):
            raise HaNotAllowed()
        raise HaUnauthorized()
    if call.kind == KIND_STATE:
        if call.entity_ids[0] not in ha_allowlist.readable_entities(settings, profiles):
            raise HaNotAllowed()
        return Authorized(source)
    if call.kind == KIND_CAMERA:
        if call.entity_ids[0] not in ha_allowlist.camera_entities(settings):
            raise HaNotAllowed()
        return Authorized(source)
    configured = ha_allowlist.configured_calls(settings, profiles)
    enforce = ha_allowlist.policy_enforced(settings)
    service = call.service_id
    entries = {}
    for entity in call.entity_ids:
        key = (entity, service)
        if key not in configured:
            raise HaNotAllowed()
        entry = configured[key]
        allowed, _msg = ha_policy.permitted(entity, service, entry, source,
                                            enforce=enforce)
        if not allowed:
            raise HaNotAllowed()
        entries[entity] = entry
    return Authorized(source, entries)
