"""This device's own programs: the kitchen screen, the Stream Deck and the reader.

Before 0.21 the app trusted every request from 127.0.0.1. Now each program on
the device proves itself with something only root on this device can make,
derived from the host bridge's token (<data_dir>/bridge-token):

- agent keys for the Stream Deck and the sensor reader:
  'pra1.' + agent + '.' + hex HMAC-SHA256(token, 'pr-agent-v1' LF agent);
- a single-use kiosk ticket the screen's launcher hands the browser:
  'v1.' + t + '.' + n + '.' + hex HMAC-SHA256(token, 'pr-kiosk-v1' LF t LF n),
  at most 180 seconds old and used once.

The old trust (legacy loopback) ends on its own once every installed program
has proven itself and nothing else has needed it for a week (the retirement
rule in evaluate_retirement). A server with none of these programs never
keeps it (own_programs_possible). Nothing reopens it automatically.

The test vectors are in tests/fixtures/local_credentials_vectors.json; the
host helpers (foodassistant-agent-key, foodassistant-kiosk-ticket) compute the
same values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import auth_state, bridge
from .state_lock import state_write_lock

log = logging.getLogger("foodassistant.local_agents")

AGENTS = ("streamdeck", "gadgets")
AGENT_LABEL = b"pr-agent-v1"
KIOSK_LABEL = b"pr-kiosk-v1"
TICKET_MAX_AGE = 180
TICKET_MAX_AHEAD = 30
NONCE_KEEP_SECONDS = 600
MAX_NONCES = 200
REREAD_SECONDS = 30.0
RELAUNCH_GAP = 600.0
RELAUNCH_PER_DAY = 3
DAY = 86400.0
_HOST_BRIDGE = "http://127.0.0.1:9299"
NONCE_FILE = "kiosk-launch-nonces.json"

_AGENT_KEY_RE = re.compile(r"^pra1\.(streamdeck|gadgets)\.([0-9a-f]{64})$")
_TICKET_RE = re.compile(r"^v1\.([0-9]{9,11})\.([0-9a-f]{32})\.([0-9a-f]{64})$")

_lock = threading.Lock()
_state: dict = {
    "last_reread": None,
    "nonces": {},          # in-memory fallback when data_dir is unwritable
    "hw": None,            # last good /hardware/status answer
    "deck_push_started": False,
    "memory_only": False,
}


def reset(persist: bool = True) -> None:
    """Forget every cached value (tests). persist=False keeps nonces in memory only."""
    with _lock:
        _state["last_reread"] = None
        _state["nonces"] = {}
        _state["hw"] = None
        _state["deck_push_started"] = False
        _state["memory_only"] = not persist


# --- Agent keys --------------------------------------------------------------


def _agent_key_for(token: str, agent: str) -> str:
    mac = hmac.new(token.encode("utf-8"), AGENT_LABEL + b"\n" + agent.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"pra1.{agent}.{mac}"


def agent_key(agent: str) -> str | None:
    """The agent key for ``agent``, or None without a bridge token."""
    if agent not in AGENTS:
        return None
    token = bridge.bridge_token()
    if not token:
        return None
    return _agent_key_for(token, agent)


def _fresh_token_allowed() -> bool:
    now = time.monotonic()
    with _lock:
        last = _state["last_reread"]
        if last is not None and now - last < REREAD_SECONDS:
            return False
        _state["last_reread"] = now
        return True


def match_agent_key(secret: str) -> str | None:
    """The agent a presented 'pra1.' key belongs to, or None."""
    m = _AGENT_KEY_RE.match(secret or "")
    if not m:
        return None
    agent = m.group(1)
    token = bridge.bridge_token()
    if token and hmac.compare_digest(_agent_key_for(token, agent), secret):
        return agent
    # The token may have changed (a bridge restart, a restore): read it again,
    # at most every 30 seconds.
    if _fresh_token_allowed():
        token = bridge.read_bridge_token_fresh()
        if token and hmac.compare_digest(_agent_key_for(token, agent), secret):
            return agent
    return None


# --- Kiosk tickets -----------------------------------------------------------


def make_ticket(token: str, t, n: str) -> str:
    t = str(int(t))
    mac = hmac.new(token.encode("utf-8"),
                   KIOSK_LABEL + b"\n" + t.encode("ascii") + b"\n" + n.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"v1.{t}.{n}.{mac}"


def _nonce_path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / NONCE_FILE


def _load_nonces() -> dict:
    try:
        data = json.loads(_nonce_path().read_text())
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()
                    if isinstance(v, (int, float))}
    except (OSError, ValueError, TypeError):
        pass
    return dict(_state["nonces"])


def _save_nonces(nonces: dict) -> None:
    path = _nonce_path()
    try:
        fd, tmp = tempfile.mkstemp(prefix=".kiosk-nonces.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(nonces, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass
    _state["nonces"] = dict(nonces)


def _use_nonce(n: str, now: float) -> bool:
    """Record a nonce; False when it was already used."""
    if _state["memory_only"]:
        with _lock:
            nonces = {k: v for k, v in _state["nonces"].items() if now - v < NONCE_KEEP_SECONDS}
            if n in nonces:
                return False
            nonces[n] = now
            if len(nonces) > MAX_NONCES:
                for k in sorted(nonces, key=nonces.get)[:len(nonces) - MAX_NONCES]:
                    nonces.pop(k, None)
            _state["nonces"] = nonces
            return True
    with state_write_lock(_nonce_path()):
        nonces = {k: v for k, v in _load_nonces().items() if now - v < NONCE_KEEP_SECONDS}
        if n in nonces:
            return False
        nonces[n] = now
        if len(nonces) > MAX_NONCES:
            for k in sorted(nonces, key=nonces.get)[:len(nonces) - MAX_NONCES]:
                nonces.pop(k, None)
        _save_nonces(nonces)
        return True


def _ticket_mac_ok(token: str, t: str, n: str, mac: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(make_ticket(token, t, n).rsplit(".", 1)[1], mac)


def verify_ticket(ticket: str, now=None) -> bool:
    """True for a fresh, correctly signed ticket seen for the first time."""
    m = _TICKET_RE.match(ticket or "")
    if not m:
        return False
    t_text, n, mac = m.groups()
    t_now = time.time() if now is None else float(now)
    t = int(t_text)
    if t < t_now - TICKET_MAX_AGE or t > t_now + TICKET_MAX_AHEAD:
        return False
    ok = _ticket_mac_ok(bridge.bridge_token(), t_text, n, mac)
    if not ok and _fresh_token_allowed():
        ok = _ticket_mac_ok(bridge.read_bridge_token_fresh(), t_text, n, mac)
    if not ok:
        return False
    return _use_nonce(n, t_now)


# --- Proofs ------------------------------------------------------------------


def _agent_node(agent: str) -> dict:
    return ((auth_state.load().get("agents") or {}).get(agent)) or {}


def deck_proven(now=None, days: float = 30) -> dict | None:
    """{'origin', 'at'} when the deck used its key with navorigin1 within
    ``days`` days, else None. The origin is scheme://Host the deck reached
    the app at (services/deck_config.py builds the proxy form from it)."""
    t = time.time() if now is None else float(now)
    node = _agent_node("streamdeck")
    ts = auth_state.parse_ts(node.get("nav_last"))
    if ts is None or ts < t - days * DAY:
        return None
    return {"origin": str(node.get("origin") or ""), "at": node.get("nav_last")}


def deck_origin() -> str:
    return str(_agent_node("streamdeck").get("origin") or "")


def gadgets_proven(now=None, days: float = 1) -> bool:
    t = time.time() if now is None else float(now)
    ts = auth_state.parse_ts(_agent_node("gadgets").get("last"))
    return ts is not None and ts >= t - days * DAY


def screen_proven(now=None, days: float = 7) -> bool:
    t = time.time() if now is None else float(now)
    ts = auth_state.parse_ts(_agent_node("screen").get("last_ticket"))
    return ts is not None and ts >= t - days * DAY


def record_agent_use(agent: str, caps=(), version: str = "", origin_url: str = "") -> None:
    """Note an agent key use; the deck's first proof starts one config push."""
    try:
        was_proven = agent == "streamdeck" and bool(deck_proven())
        auth_state.record_agent(agent, caps=caps, origin=origin_url, version=version)
        if agent == "streamdeck" and "navorigin1" in (caps or ()) and not was_proven:
            with _lock:
                if _state["deck_push_started"]:
                    return
                _state["deck_push_started"] = True
            threading.Thread(target=_push_deck_quietly, daemon=True).start()
    except Exception:
        log.debug("record_agent_use failed", exc_info=True)


def _push_deck_quietly() -> None:
    try:
        from . import deck_config
        deck_config.push_to_deck()
    except Exception:
        pass


# --- The host bridge ---------------------------------------------------------


def bridge_present() -> bool:
    try:
        return bridge.bridge_token_path().exists()
    except OSError:
        return False


async def bridge_hardware_status():
    """GET /hardware/status from the bridge (token-exempt), None on failure."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{_HOST_BRIDGE}/hardware/status")
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict):
            return None
    except Exception:
        return None
    _state["hw"] = data
    return data


def last_hardware_status():
    return _state["hw"]


def kiosk_installed():
    """True or False from the last bridge answer, None when unknown."""
    hw = _state["hw"]
    if not isinstance(hw, dict):
        return None
    kiosk = hw.get("kiosk")
    if not isinstance(kiosk, dict) or "installed" not in kiosk:
        return None
    return bool(kiosk.get("installed"))


def _installed(hw: dict, key: str):
    node = hw.get(key) if isinstance(hw, dict) else None
    if not isinstance(node, dict) or "installed" not in node:
        return None
    return bool(node.get("installed"))


OWN_PROGRAMS = ("kiosk", "streamdeck", "gadgets")


def own_programs_possible(hw=None) -> bool:
    """Whether this device runs programs of its own (the kitchen screen, the
    Stream Deck, the sensor reader) that may still lean on the old trust.

    A Raspberry Pi appliance always may. Any other install only when the host
    bridge says one of them is installed. A server with none of them has no
    program of its own on loopback, and anything that reaches the app there
    without a key or a forwarding header is far more likely a reverse proxy on
    the same machine: from 1.30 nginx on its defaults sends a request that is
    byte for byte what a local program sends, so loopback alone proves
    nothing."""
    try:
        from ..config import settings
        if settings.is_pi_appliance():
            return True
    except Exception:
        return True
    hw = hw if isinstance(hw, dict) else _state["hw"]
    if not isinstance(hw, dict):
        return False
    return any(_installed(hw, key) for key in OWN_PROGRAMS)


def evaluate_retirement(now=None, hw=None) -> str:
    """Apply the hourly retirement rule: 'kept', 'retired' or 'confirm'."""
    t = time.time() if now is None else float(now)
    lt = auth_state.loopback_trust()
    if lt.get("state") != "legacy":
        return "retired"
    # A server with none of the device's own programs ends the old trust at
    # once (access.snapshot already ignores it there). With a bridge that has
    # not answered yet, wait for its answer before deciding for good.
    if ((not bridge_present() or isinstance(hw, dict))
            and not own_programs_possible(hw)):
        auth_state.retire("auto", now=t)
        return "retired"
    doc = auth_state.load()
    uses = doc.get("legacy_use") or {}

    def _recent(cls, days):
        ts = auth_state.parse_ts((uses.get(cls) or {}).get("last"))
        return ts is not None and ts >= t - days * DAY

    # Rule (b) needs a week of watching: uses were only recorded from this
    # version on, so an empty record on the day of the update says nothing
    # about a program that calls once a day or once a week.
    since = (auth_state.parse_ts(lt.get("since"))
             or auth_state.parse_ts(doc.get("first_start")) or t)
    watched = t - since >= 7 * DAY
    rule_b = watched and not any(_recent(c, 7) for c in ("browser", "deck", "reader", "other"))
    rule_c = not _recent("other", 90)
    if not bridge_present():
        if rule_b:
            auth_state.retire("auto", now=t)
            return "retired"
        return "kept"
    if not isinstance(hw, dict):
        return "kept"
    rule_a = True
    if _installed(hw, "kiosk") and not screen_proven(t, 7):
        rule_a = False
    if _installed(hw, "streamdeck") and not deck_proven(t, 7):
        rule_a = False
    gadgets = _installed(hw, "gadgets")
    if gadgets is None or (gadgets and not gadgets_proven(t, 1)):
        rule_a = False
    if rule_a and rule_b and rule_c:
        auth_state.retire("auto", now=t)
        return "retired"
    if rule_a and rule_b:
        if not lt.get("confirm_needed"):
            auth_state.set_confirm_needed(True, now=t)
            from .access import LOCAL_TRUST_CONFIRM, LOCAL_TRUST_CONFIRM_ITEM_TITLE, raise_item
            raise_item("local_trust_confirm", LOCAL_TRUST_CONFIRM_ITEM_TITLE,
                       LOCAL_TRUST_CONFIRM, "local_trust_confirm")
        return "confirm"
    return "kept"


def unproven_agents(hw=None, now=None) -> list[str]:
    """Installed programs that have not signed in with their own key yet."""
    hw = hw if isinstance(hw, dict) else _state["hw"]
    out = []
    if not isinstance(hw, dict):
        return out
    if _installed(hw, "kiosk") and not screen_proven(now, 7):
        out.append("screen")
    if _installed(hw, "streamdeck") and not deck_proven(now, 7):
        out.append("streamdeck")
    if _installed(hw, "gadgets") and not gadgets_proven(now, 1):
        out.append("gadgets")
    return out


async def request_kiosk_relaunch(reason: str = "") -> bool:
    """Ask the bridge to restart the kiosk, at most once per 10 minutes and 3 times a day."""
    t = time.time()
    try:
        today = auth_state.relaunches_today(t)
        if len(today) >= RELAUNCH_PER_DAY:
            return False
        if today and t - today[-1] < RELAUNCH_GAP:
            return False
        auth_state.record_relaunch(t)
        async with bridge.bridge_client(timeout=10) as client:
            r = await client.post(f"{_HOST_BRIDGE}/kiosk/restart", json={})
        log.info("Asked the bridge to restart the kitchen screen (%s): %s",
                 reason or "requested", r.status_code)
        return r.status_code == 200
    except Exception:
        log.debug("kiosk relaunch failed", exc_info=True)
        return False


async def refresh_deck_config_if_key_changed() -> None:
    """Push the deck config when a proven deck has no proxy push or the key changed."""
    try:
        from . import deck_config
        last_push = getattr(deck_config, "last_push", None)
        push = getattr(deck_config, "push_to_deck", None)
        if last_push is None or push is None:
            return
        key = agent_key("streamdeck")
        kh = hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""
        lp = last_push()
        need = False
        if deck_proven() and (lp is None or lp.get("form") != "proxy"):
            need = True
        elif lp and lp.get("form") == "proxy" and lp.get("key_sha256") != kh:
            need = True
        if need:
            import anyio
            await anyio.to_thread.run_sync(push)
    except Exception:
        log.debug("deck config refresh failed", exc_info=True)


# --- Kiosk URL check (D7) ----------------------------------------------------


def _kiosk_url_problem(hw) -> str:
    """The host the screen opens when it is not this kitchen's local address, else ''."""
    k = (hw or {}).get("kiosk") if isinstance(hw, dict) else None
    if not isinstance(k, dict) or not k.get("installed") or k.get("url_local") is not False:
        return ""
    host = str(k.get("url_host") or "").strip().lower()
    try:
        from ..config import settings
        if settings.is_satellite() and host:
            server = (urlsplit(settings.remote_server_url or "").hostname or "").lower()
            if server and host == server:
                return ""
    except Exception:
        pass
    try:
        from .access import own_loopback_names
        if host and "." not in host and host in own_loopback_names():
            return ""
    except Exception:
        pass
    return host or "another address"


def check_kiosk_url(hw) -> None:
    """Raise one inbox item when the screen opens this kitchen through another address."""
    try:
        host = _kiosk_url_problem(hw)
        if not host:
            return
        from .access import KIOSK_URL_ITEM_BODY, KIOSK_URL_ITEM_TITLE, raise_item
        raise_item("kiosk_url_not_local", KIOSK_URL_ITEM_TITLE,
                   KIOSK_URL_ITEM_BODY.format(host=host), f"kiosk_url_not_local:{host}",
                   level="warning")
    except Exception:
        log.debug("kiosk url check failed", exc_info=True)


def kiosk_url_status() -> dict:
    hw = _state["hw"]
    k = (hw or {}).get("kiosk") if isinstance(hw, dict) else None
    if not isinstance(k, dict) or "url_local" not in k:
        return {"local": None, "host": ""}
    host = _kiosk_url_problem(hw)
    if host:
        return {"local": False, "host": host}
    return {"local": True if k.get("url_local") is not None else None,
            "host": str(k.get("url_host") or "")}
