"""Detection-to-pop-up decision logic, and Reolink AI-state / device-info
parsing (FoodAssistant-akd0, FoodAssistant-qft4).

A camera pop-up channel already exists (``ha_events.add_camera`` /
``POST /events/camera-popup``, rendered by ``static/js/ha-events.js``). This
module is the pure logic that decides WHEN to use it:

* ``should_popup`` / ``POST /events/camera-detect`` let a single Home
  Assistant automation post every detection it sees (person, vehicle,
  animal, a doorbell visitor) without per-type conditions in HA; the
  on/off choice lives in each camera's "pop up on" setting here.
* ``reolink_ai_detections`` / ``reolink_popup_decision`` parse a Reolink
  camera's ``GetAiState`` CGI reply for a best-effort poller that drives the
  same channel without any Home Assistant automation at all.
* ``reolink_capabilities`` reads a Reolink ``GetDevInfo`` reply to tell a
  doorbell apart from a plain camera and flag two-way-talk capable models
  (FoodAssistant-qft4). The talk audio path itself needs a WebRTC session and
  is not implemented here; this only surfaces the capability.

Every function here is pure (no network, no settings) so the decision logic
is fully unit-testable.
"""
from __future__ import annotations

# Canonical detection types the pop-up settings and both trigger paths (Home
# Assistant and the Reolink poller) agree on. "visitor" is a doorbell button
# press, not an AI detection, but it rides the same list and the same
# enable/disable toggle per camera.
DETECTION_TYPES = ("person", "vehicle", "animal", "visitor")


def should_popup(detection_type: str, enabled_types) -> bool:
    """True when a detection of this type should pop the camera up on screen.

    ``enabled_types`` is whatever the user turned on for this camera (a
    list/set/tuple of strings, matched case-insensitively). A blank or
    unrecognised type never pops up (fail closed): only a type the user
    explicitly enabled triggers a pop-up, so a stray/uncategorised detection
    from Home Assistant never surprises the kiosk.
    """
    t = (detection_type or "").strip().lower()
    if not t:
        return False
    enabled = {str(e).strip().lower() for e in (enabled_types or [])}
    return t in enabled


# --- Reolink AI-state parsing (FoodAssistant-akd0) --------------------------
# A Reolink camera with AI detection exposes ``cmd=GetAiState`` on the same CGI
# API the snapshot uses. A typical reply looks like:
#   {"value": {"channel": 0, "people": {"alarm_state": 1},
#              "vehicle": {"alarm_state": 0}, "dog_cat": {"alarm_state": 0},
#              "face": {"alarm_state": 0}, "visitor": {"alarm_state": 0}}}
# Reolink's batch CGI form sometimes wraps this as [{"value": ...}]. Both
# shapes are handled below.
_REOLINK_AI_MAP = {
    "people": "person",
    "person": "person",
    "face": "person",
    "vehicle": "vehicle",
    "dog_cat": "animal",
    "animal": "animal",
    "visitor": "visitor",   # doorbell button press (FoodAssistant-qft4)
    "ring": "visitor",
}


def reolink_ai_detections(state: dict) -> list[str]:
    """Normalized, deduplicated detection types alarming in a Reolink AI-state
    reply (sorted for a stable, testable order).

    Accepts the raw ``GetAiState`` JSON, a dict or the ``[{"value": ...}]``
    list form. A malformed or unrecognised reply yields an empty list rather
    than raising, since a best-effort poller must never crash on a firmware
    quirk or a camera that does not support AI detection at all.
    """
    if isinstance(state, list):
        state = state[0] if state and isinstance(state[0], dict) else {}
    if not isinstance(state, dict):
        return []
    value = state.get("value", state)
    if not isinstance(value, dict):
        return []
    found: set[str] = set()
    for key, sub in value.items():
        mapped = _REOLINK_AI_MAP.get(str(key).strip().lower())
        if not mapped:
            continue
        alarmed = False
        if isinstance(sub, dict):
            try:
                alarmed = int(sub.get("alarm_state", 0)) == 1
            except (TypeError, ValueError):
                alarmed = False
        elif isinstance(sub, (int, str, bool)):
            alarmed = _truthy(sub)
        if alarmed:
            found.add(mapped)
    return sorted(found)


def reolink_popup_decision(state: dict, enabled_types) -> tuple[bool, list[str]]:
    """(should_popup, detected_types) for one Reolink AI-state poll.

    Pops up when ANY currently-alarming type is in the camera's enabled set,
    so (for example) an unrelated vehicle alarm alongside a person detection
    still pops the camera up when only "person" is enabled.
    """
    detected = reolink_ai_detections(state)
    popup = any(should_popup(t, enabled_types) for t in detected)
    return popup, detected


# --- Reolink doorbell + capability parsing (FoodAssistant-qft4) -------------
# A doorbell's model/type comes back through Reolink's ``GetDevInfo`` (or the
# same fields nested under "DevInfo" in the batch CGI form). We only need
# enough of that reply to tell a doorbell apart from a plain camera, and
# whether the model carries two-way audio.

_DOORBELL_HINTS = ("doorbell",)


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def reolink_capabilities(dev_info: dict) -> dict:
    """{"is_doorbell": bool, "two_way_talk": bool} from a Reolink device-info
    reply (``GetDevInfo``, optionally wrapped in ``{"value": {...}}``).

    Reads the ``type``/``model``/``name``/``deviceType`` fields
    case-insensitively; any of them naming a doorbell model marks the camera
    as a doorbell. Every Reolink doorbell carries a microphone and speaker, so
    a doorbell is always two-way-talk capable; a plain camera is also flagged
    when the reply itself claims audio-talk support (``audioTalk`` /
    ``supportAudioTalk``, on the models that have it). The talk *feature*
    (actually opening a two-way audio session) is not implemented here or
    anywhere in this app yet: it needs a WebRTC session the app does not have
    a transport for, so this only surfaces the capability flag for later use
    and for the setup pane to show "supports two-way talk".
    """
    if not isinstance(dev_info, dict):
        return {"is_doorbell": False, "two_way_talk": False}
    value = dev_info.get("value", dev_info)
    if isinstance(value, list):
        value = value[0] if value and isinstance(value[0], dict) else {}
    if not isinstance(value, dict):
        value = {}
    dev = value.get("DevInfo", value)
    if not isinstance(dev, dict):
        dev = value
    text = " ".join(str(dev.get(k, "")) for k in
                    ("type", "model", "name", "deviceType")).lower()
    is_doorbell = any(hint in text for hint in _DOORBELL_HINTS)
    two_way_talk = (is_doorbell or _truthy(dev.get("audioTalk"))
                    or _truthy(dev.get("supportAudioTalk")))
    return {"is_doorbell": is_doorbell, "two_way_talk": two_way_talk}
