"""Who is asking, and may they do this?

Every request gets one principal, worked out once by the auth middleware
(main.require_auth) and kept on request.state: a signed-in session, a key,
one of this device's own programs (the Stream Deck and the sensor reader sign
in with agent keys), the kitchen screen's own session, the old trust for
programs on this device (legacy loopback), or nobody. Where the request came
from (its origin) is decided from the connection and its headers, never from
the client address alone, so a reverse proxy on the same machine is not
mistaken for the kitchen screen.

The rules are pure functions over plain values (classify_origin_raw,
route_family, allows_raw, decide), so they can be tested as tables without
HTTP; the thin Request wrappers feed them.

Copy shown to people lives here too, so each message has one home.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from . import lan

log = logging.getLogger("foodassistant.access")

# --- Constants ---------------------------------------------------------------

FORWARDING_HEADERS = (
    "forwarded", "via", "x-real-ip", "cf-connecting-ip", "true-client-ip",
    "x-client-ip", "fastly-client-ip",
)
# Fixed private ranges. Never ipaddress.is_private: on Python 3.12 it calls
# the documentation ranges (192.0.2.0/24 and friends) private, and those must
# count as the internet here.
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
))
INGRESS_PEER = "172.30.32.2"
# The Forager remote-access tunnel's address range (cloud/app/tunnel.py
# TUNNEL_CIDR; the hub is 10.99.0.1). A request from the hub came in from the
# internet through Forager, so it is proxied, never the home network, even
# though the range sits inside 10.0.0.0/8 and the hub adds no forwarding
# header of its own.
FORAGER_TUNNEL_NETWORK = ipaddress.ip_network("10.99.0.0/16")
# Name endings only a home network hands out. Deliberately not .fritz.box:
# .box is a public top-level domain, so such a name can be registered
# outside the home and serve a DNS-rebinding page.
HOME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal",
                 ".localdomain", ".intranet", ".private", ".corp")
FAMILIES = ("ha", "events", "reader", "display")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HOME_ORIGINS = frozenset({"lan_direct", "plain_loopback", "ingress"})
OUTSIDE_ORIGINS = frozenset({"proxied", "public"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_CLIENT_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_CAP_RE = re.compile(r"^[a-z0-9]{1,16}$")

# --- Copy --------------------------------------------------------------------

OPEN_REFUSAL_MSG = (
    "This kitchen has no password, so it only answers devices on your home "
    "network. To use it from outside, it needs a password and two-factor "
    "sign-in: set both up in Settings, Security from a device at home, or use "
    "the setup code.")
ADMIN_KEY_MSG = ("API keys cannot change settings. Sign in, or create an admin "
                 "key in Settings, Security.")
SCOPE_MSG = "This key is not allowed to do that."
OPEN_NOTICE = ("No password: anyone on your home network can use and change "
               "this kitchen. Set a password.")
VIEWER_MSG = "This needs the admin password."
SATELLITE_GATE_MSG = (
    "This device has no password yet, so it only allows this from its own "
    "screen. Add a password in Settings to use it from anywhere.")
NAME_REFUSAL_MSG = (
    "This kitchen has no password, so it only answers the names and addresses "
    "of your home network. Open it by its home address, or set a password in "
    "Settings, Security.")
NAME_ALLOW_HINT = (
    "To keep using this name, add it under Other names for this kitchen in "
    "Settings, Security, from a device at home.")
CROSS_SITE_MSG = ("That request came from another site, so it was refused. "
                  "Open Pantry Raider directly and try again.")
SETUP_CODE_MSG = ("This kitchen was reached from outside your home network. "
                  "Enter its setup code to continue.")
SETUP_CODE_INTRO = (
    "This kitchen was reached from outside your home network. To set it up "
    "from here, enter the setup code from its log and choose a password.")
SETUP_CODE_INTRO_HAS_PASSWORD = (
    "This kitchen was reached from outside your home network. To set it up "
    "from here, enter the setup code from its log.")
SETUP_CODE_INTRO_OPEN = (
    "This kitchen has no password, so it only answers devices on your home "
    "network. To use it from here, enter the setup code from its log and "
    "choose a password.")
SETUP_CODE_INTRO_RESCUE = (
    "Signing in from outside your home network needs two-factor, which is not "
    "turned on for this kitchen yet. Enter the setup code from its log and "
    "your password to sign in once, then turn two-factor on.")
SETUP_CODE_WHERE = (
    "The code is in this kitchen's log, on the newest line that begins with "
    "Pantry Raider setup code. With Docker Compose or on a Raspberry Pi, run "
    "sudo docker logs foodassistant-service 2>&1 | grep \"setup code\". On "
    "Unraid, open the Docker tab, click PantryRaider and choose Logs. In an "
    "add-on, open the add-on's Log tab.")
SETUP_CODE_WRONG_MSG = ("That code did not match. Check the log for the newest "
                        "code and try again.")
SETUP_CODE_SLOW_MSG = "Too many tries. Wait a minute and try again."
PASSWORD_SHORT_MSG = "Choose a password with at least 8 characters."
PASSWORD_MISMATCH_MSG = "The two passwords do not match."
RESCUE_2FA = (
    "Signing in from outside your home network needs two-factor as well as a "
    "password. After the setup code signs you in, this kitchen takes you "
    "straight to turning two-factor on.")
RESCUE_ENV = (
    "On the server itself, you can set AUTH_PASSWORD in the container settings "
    "and restart the container. Signing in from outside your home network then "
    "also needs two-factor: sign in once with the setup code and that password "
    "(the sign-in page offers it), then turn two-factor on.")
RESCUE_LAN_CIDRS = (
    "If your home network uses addresses this kitchen does not recognize, add "
    "them to LAN_CIDRS in the container settings, or under Home networks in "
    "Settings, Security.")
RESCUE_PASSWORD_WRONG_MSG = ("That password did not match. Enter the main "
                             "password for this kitchen.")
RESCUE_CODE_USED_UP_MSG = (
    "That password did not match, and this setup code has had too many wrong "
    "passwords, so it no longer works. Open this page again for a new code, "
    "then find it in the log.")
SETUP_CODE_LINK = "Use the setup code instead"
TWO_FACTOR_NEXT_MSG = (
    "Turn on two-factor now. Signing in from outside your home network needs "
    "it, and without it the next sign-in from outside needs the setup code "
    "again.")
OUTSIDE_ORDER_MSG = (
    "You are not on your home network. Turn on two-factor below before you set "
    "a password: once a password is set, signing in from outside needs both.")
GRACE_ITEM_TITLE = "This kitchen will stop answering through a proxy on {date}"
GRACE_ITEM_BODY = (
    "Someone reached this kitchen through a proxy or tunnel at {host}. It has "
    "no password, so from {date} it stops answering there. To keep that "
    "access, turn on two-factor sign-in in Settings, Security, then set a "
    "password.")
NAME_ITEM_TITLE = "This kitchen was opened by a name it does not know"
NAME_ITEM_BODY = (
    "Someone opened this kitchen at {name}. It has no password, so from {date} "
    "it stops answering that name. To keep using it, add {name} under Other "
    "names for this kitchen in Settings, Security, or set a password.")
KIOSK_URL_ITEM_TITLE = "The kitchen screen opens this kitchen through another address"
KIOSK_URL_ITEM_BODY = (
    "The screen on this device opens {host} instead of localhost, the address "
    "where it signs itself in. Through any other address it signs in like a "
    "visitor from outside and may stop at the sign-in page. To point it at "
    "localhost, run sudo foodassistant-host-harden kiosk-url-local on the "
    "device. If the screen is meant to show a different kitchen, dismiss this.")
KIOSK_URL_WARNING = (
    "The screen on this device opens {host} instead of localhost, so it cannot "
    "sign itself in. Run sudo foodassistant-host-harden kiosk-url-local on the "
    "device to point it at localhost.")
RELAUNCH_NOTE = "Signing this screen in again."
RELAUNCH_LOCAL_MSG = "Only this device's own screen can do that."
MINT_PASSWORD_MSG = "Enter your current password to create a key."
ADMIN_KEY_OPEN_MSG = (
    "Set a password first. An admin key keeps working after a password is set, "
    "so it can only be created on a kitchen that has one.")
ENV_KEY_MSG = ("This key comes from the container settings (API_KEY). Remove it "
               "there and restart to turn it off.")
EXCHANGE_LEGACY_MSG = "Only an older full-access key can be swapped for a new one."
EXCHANGE_LAN_MSG = "Swap keys from your home network."
EXCHANGE_RATE_MSG = "Too many key swaps. Try again in an hour."
LAN_CIDRS_REMOTE_MSG = "Change home networks from a device on your home network."
KEY_SHOWN_ONCE = "Copy this key now. It is not shown again."
REQUIRE_PASSWORD_HELP = (
    "A password is required by default. Turn it off only for the Home "
    "Assistant add-on, where Home Assistant signs people in. Behind any other "
    "proxy or tunnel this kitchen needs its own password, and signing in from "
    "outside also needs two-factor.")
KEYLESS_LABELS = {
    "ha": "Home Assistant",
    "events": "Home Assistant",
    "display": "Bandit Cubs and small displays",
    "esp": "ESP buttons and screens",
    "reader": "Sensor readers",
}
KEYLESS_DEVICES_WARNING = (
    "These devices at home use this kitchen without a key: {labels}. Once it "
    "has a password they stop working until each one has a key. Approve their "
    "pairing, or create a key for each in Settings, Security.")
KEYLESS_REFUSED_WARNING = (
    "These devices at home were turned away in the last 7 days because they "
    "sent no key: {labels}. Approve their pairing, or create a key for each in "
    "Settings, Security.")
KEYLESS_SETUP_CODE_NOTE = (
    "Some devices at home use this kitchen without a key. Once it has a "
    "password they stop working until each one has a key. After you sign in, "
    "the Review inbox lists them.")
KEYLESS_ITEM_TITLE = "Devices at home need a key now"
KEYLESS_ITEM_BODY = (
    "Some devices at home used this kitchen without a key: {labels}. Now that "
    "it has a password they need one. Approve their pairing, or create a key "
    "for each in Settings, Security.")
SAME_HOST_PROXY_ITEM_TITLE = "A proxy on this server hides where visitors come from"
SAME_HOST_PROXY_ITEM_BODY = (
    "Requests reach this kitchen through a reverse proxy on this server that "
    "does not say where they come from, so they can look like this server's "
    "own programs or a device at home. This kitchen treats the ones it can "
    "recognize as visitors from outside, and signing in through the proxy "
    "needs a password and two-factor. To make sure every request is "
    "recognized, have the proxy pass "
    "on the visitor's address and the name they opened. For nginx, add these "
    "two lines next to proxy_pass and reload it: proxy_set_header Host $host; "
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
SAME_HOST_PROXY_WARNING = (
    "A reverse proxy on this server does not say where visitors come from. "
    "Have it pass Host and X-Forwarded-For (for nginx: proxy_set_header Host "
    "$host; and proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;).")
KEY_ADMIN_LOOPBACK_ITEM_TITLE = "A program on this device changes settings with a key"
KEY_ADMIN_LOOPBACK_ITEM_BODY = (
    "A program on this device used the key \"{name}\" ({client}) to change "
    "settings. That works for now because programs on this device are still "
    "trusted, but it stops once they all sign in with their own keys. Create an "
    "admin key for it in Settings, Security.")
LOCAL_TRUST_ON = (
    "Programs on this device can connect without a key for now. This ends on "
    "its own once the screen, the Stream Deck and the sensor reader all sign "
    "in with their own keys and a week passes without anything else needing "
    "it.")
LOCAL_TRUST_OFF = "Every program on this device signs in with its own key."
LOCAL_TRUST_WAITING = ("Waiting for the screen, the Stream Deck and the sensor "
                       "reader to sign in with their own keys.")
LOCAL_TRUST_CONFIRM = (
    "Everything that belongs to this kitchen signs in on its own now, but other "
    "programs on this device still connect without a key. Give them a key, or "
    "require every program to sign in now.")
LOCAL_TRUST_CONFIRM_ITEM_TITLE = "Programs on this device still connect without a key"
KIOSK_ADMIN_HELP = ("When this is off, the kitchen screen can use every kitchen "
                    "page but not Settings. Open Settings from another device.")
SATELLITE_KEYS_HELP = (
    "Older satellites need your AI, recipe and Home Assistant keys on the "
    "device itself. Newer ones ask this kitchen instead. Auto shares them "
    "while a satellite seen in the last 30 days still needs them. A newly set "
    "up kitchen shares nothing until such a satellite is seen.")
MIRROR_HELP = (
    "Keeps a copy of each new key where older versions of Pantry Raider look "
    "for keys, so devices keep working if you go back to an older version. "
    "Turning this off removes the copies. Keys created while it is off only "
    "work on this version and newer.")
AUTO_RETIRE_HELP = (
    "An older full-access key is removed 90 days after its last use, once "
    "every device that used it has its own key. Keys used by a satellite or a "
    "Bandit Cub are never removed without asking you.")
GATEWAY_HINT = (
    "Some requests reach this kitchen through Docker's network gateway, so it "
    "cannot tell whether they come from your home network. Usually that is "
    "another container on this server, such as Home Assistant, connecting "
    "through the server's address, or a visitor using IPv6. A password removes "
    "the doubt. For IPv6 visitors, check the port line in your compose file: "
    "if it does not start with 0.0.0.0, change it to 0.0.0.0:9284:8000 and "
    "run docker compose up -d.")
# Unraid's template publishes the port on every address and has no setting to
# change that, so the compose advice above does not apply there.
GATEWAY_HINT_UNRAID = (
    "Some requests reach this kitchen through Docker's network gateway, so it "
    "cannot tell whether they come from your home network. On Unraid that "
    "covers anyone who opens it over IPv6 and other containers on this "
    "server, such as Home Assistant, and Unraid has no setting that changes "
    "it. Set a password in Settings, Security so everyone signs in.")
KEY_ADMIN_ITEM_TITLE = "A key tried to change settings"
KEY_ADMIN_ITEM_BODY = (
    "A program using the key \"{name}\" ({client}) tried to change settings and "
    "was refused. Keys can no longer change settings. If this is your own "
    "script, create an admin key for it in Settings, Security.")
# Each inbox item's title and body together, for callers that want both.
GRACE_ITEM = {"title": GRACE_ITEM_TITLE, "body": GRACE_ITEM_BODY}
NAME_ITEM = {"title": NAME_ITEM_TITLE, "body": NAME_ITEM_BODY}
KIOSK_URL_ITEM = {"title": KIOSK_URL_ITEM_TITLE, "body": KIOSK_URL_ITEM_BODY}
KEYLESS_ITEM = {"title": KEYLESS_ITEM_TITLE, "body": KEYLESS_ITEM_BODY}
SAME_HOST_PROXY_ITEM = {"title": SAME_HOST_PROXY_ITEM_TITLE, "body": SAME_HOST_PROXY_ITEM_BODY}
KEY_ADMIN_LOOPBACK_ITEM = {"title": KEY_ADMIN_LOOPBACK_ITEM_TITLE,
                           "body": KEY_ADMIN_LOOPBACK_ITEM_BODY}
KEY_ADMIN_ITEM = {"title": KEY_ADMIN_ITEM_TITLE, "body": KEY_ADMIN_ITEM_BODY}

SCOPE_LABELS = {
    "admin": "Everything, including Settings",
    "legacy": "Everything except Settings (an older key)",
    "satellite": "A second screen (satellite)",
    "ha": "The Home Assistant integration",
    "ha_rest": "Home Assistant sensors, buttons and ESPHome panels",
    "display": "A Bandit Cub or other small display",
}
FAMILY_LABELS = {
    "ha": "Home Assistant",
    "events": "Notifications from Home Assistant",
    "reader": "Sensor reader",
    "display": "Bandit Cubs and small displays",
}

# --- Small header helpers ----------------------------------------------------


def _header_names(headers) -> list[str]:
    if headers is None:
        return []
    try:
        return [str(k).lower() for k in headers.keys()]
    except Exception:
        return []


def _get(headers, name: str, default: str = "") -> str:
    if headers is None:
        return default
    try:
        value = headers.get(name)
        if value is None and isinstance(headers, dict):
            for k, v in headers.items():
                if str(k).lower() == name:
                    value = v
                    break
    except Exception:
        return default
    return default if value is None else str(value)


def _has(headers, name: str) -> bool:
    return name in _header_names(headers)


def has_forwarding_header(headers) -> bool:
    """True when any header a reverse proxy or CDN adds is present."""
    for name in _header_names(headers):
        if name in FORWARDING_HEADERS or name.startswith("x-forwarded-"):
            return True
    return False


def unwrap_peer(peer) -> str | None:
    """The peer as a normalized IP string, or None when it is not an IP.

    TestClient's 'testclient' and a missing peer give None. An IPv4-mapped
    IPv6 address (::ffff:a.b.c.d) becomes the IPv4 address."""
    if not peer:
        return None
    try:
        ip = ipaddress.ip_address(str(peer).strip())
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def addon_runtime(environ=None) -> bool:
    """True when running as the Home Assistant add-on."""
    env = os.environ if environ is None else environ
    return env.get("PANTRY_RAIDER_ADDON") == "1" or bool(env.get("SUPERVISOR_TOKEN"))


def unraid_runtime(environ=None) -> bool:
    """True in a container that Unraid's Docker manager started.

    Unraid adds HOST_OS=Unraid to every container it runs from a template. A
    Compose Manager stack does not get it, and there the compose advice in
    GATEWAY_HINT is the right one."""
    env = os.environ if environ is None else environ
    return str(env.get("HOST_OS", "")).strip().lower() == "unraid"


def gateway_hint(environ=None) -> str:
    """The Docker-gateway notice for this install: Unraid's template cannot
    choose the address it publishes on, so it gets its own wording."""
    return GATEWAY_HINT_UNRAID if unraid_runtime(environ) else GATEWAY_HINT


def host_name(host) -> str:
    """The host from a Host header or URL, lowercased, port stripped."""
    from .request_origin import _host_only
    try:
        return _host_only(str(host or ""))
    except Exception:
        return ""


def gateway_peer_is_unclear(host) -> bool:
    """Whether a request from Docker's gateway leaves its origin in doubt.

    A browser on the server itself that opens localhost or 127.0.0.1 on the
    published port reaches the app through Docker's port forwarder, so it
    arrives from the gateway too. Its Host names loopback, which is exactly
    what the owner typed, so it is no sign of another container or an IPv6
    visitor and is no reason to warn on a brand-new install. This only decides
    whether to show the gateway notice; how the request is treated does not
    change."""
    return host_name(host) not in _LOCAL_HOSTS


def _is_local_name(name: str, own_names) -> bool:
    return bool(name) and (name in _LOCAL_HOSTS or name in (own_names or ()))


def same_host_proxy_signal_raw(headers, http_version="1.1", own_names=frozenset()) -> str:
    """Signs that a loopback request came through a proxy on this machine.

    '' when there is none, else 'http10' (the upstream protocol nginx used by
    default before 1.30), 'origin' (a browser that believes it is on the site
    in its address bar sent an Origin naming another host, or 'null') or
    'referer' (the same for a Referer). A request the browser marks
    cross-site or none is never a proxy sign: that is a page elsewhere probing
    localhost, or the kiosk's return from a store page."""
    if str(http_version or "") == "1.0":
        return "http10"
    sfs = _get(headers, "sec-fetch-site").strip().lower()
    if _has(headers, "sec-fetch-site") and sfs not in ("same-origin", "same-site"):
        return ""
    if _has(headers, "origin"):
        origin = _get(headers, "origin").strip()
        if origin.lower() == "null":
            return "origin"
        name = host_name(origin)
        if name and not _is_local_name(name, own_names):
            return "origin"
    if _has(headers, "referer"):
        name = host_name(_get(headers, "referer"))
        if name and not _is_local_name(name, own_names):
            return "referer"
    return ""


def _in_networks(ip, networks) -> bool:
    for net in networks or ():
        try:
            if ip.version == net.version and ip in net:
                return True
        except Exception:
            continue
    return False


def classify_origin_raw(peer, headers, method, host, *, addon, extra_networks,
                        own_names=frozenset(), http_version="1.1",
                        gateway_peers=frozenset(), forager_tunnel=False) -> str:
    """Where a request came from: ingress, proxied, public, plain_loopback or
    lan_direct, in that order of tests.

    A peer in the Forager tunnel range is proxied. The one way to have it
    count as home is to list it under Home networks on a kitchen that does not
    use the Forager tunnel (forager_tunnel False), for a home network that
    happens to use the same addresses.

    gateway_peers are Docker's gateway addresses when the app runs on a Docker
    bridge network. A reverse proxy on the Docker host reaches the app from
    there, so a gateway peer that shows a same-host proxy's signs (HTTP/1.0,
    or a foreign Origin or Referer, with a local Host) is proxied too."""
    if peer == INGRESS_PEER and _has(headers, "x-ingress-path") and addon:
        return "ingress"
    if has_forwarding_header(headers):
        return "proxied"
    ip_text = unwrap_peer(peer)
    if ip_text is None:
        return "public"
    ip = ipaddress.ip_address(ip_text)
    if ip_text in _LOOPBACK_PEERS:
        name = host_name(host)
        method_u = str(method or "").upper()
        sfs = _get(headers, "sec-fetch-site").strip().lower()
        fetch_ok = (method_u in SAFE_METHODS or not _has(headers, "sec-fetch-site")
                    or sfs == "same-origin")
        if (_is_local_name(name, own_names) and fetch_ok
                and same_host_proxy_signal_raw(headers, http_version, own_names) == ""):
            return "plain_loopback"
        return "proxied"
    if ip.version == 4 and ip in ipaddress.ip_network("127.0.0.0/8"):
        return "lan_direct"
    if ip.version == 4 and ip in FORAGER_TUNNEL_NETWORK and (
            forager_tunnel or not _in_networks(ip, extra_networks)):
        return "proxied"
    if (ip_text in (gateway_peers or ()) and _is_local_name(host_name(host), own_names)
            and same_host_proxy_signal_raw(headers, http_version, own_names)):
        return "proxied"
    if _in_networks(ip, LAN_NETWORKS) or _in_networks(ip, extra_networks):
        return "lan_direct"
    return "public"


# --- This device's own names -------------------------------------------------

_names_lock = threading.Lock()
_names_cache: dict = {}
_NAMES_TTL = 60.0


def _clean_name(value) -> str:
    name = str(value or "").strip().lower()
    if name.endswith("."):
        name = name[:-1]
    return name


def own_loopback_names() -> frozenset:
    """This device's own single-label host names, each also with .local.

    A program here that reaches the app by the bare host name (Debian maps it
    to 127.0.1.1) is then plain loopback like localhost. A dotted name never
    joins (DNS rebinding could produce it), and localhost is never in it.
    Cached for a minute; never raises."""
    now = time.monotonic()
    with _names_lock:
        hit = _names_cache.get("own")
        if hit is not None and now - hit[0] < _NAMES_TTL:
            return hit[1]
    raw: list[str] = []
    try:
        raw.append(socket.gethostname())
    except Exception:
        pass
    try:
        from ..config import settings
        raw.append(getattr(settings, "device_hostname", "") or "")
    except Exception:
        pass
    try:
        from .. import config as _config
        cached = getattr(_config, "_bridge_hostname_cache", None)
        if cached:
            raw.append(cached[1])
    except Exception:
        pass
    names: set[str] = set()
    for value in raw:
        name = _clean_name(value)
        if not name or name == "localhost":
            continue
        base = name[:-6] if name.endswith(".local") else name
        if not _LABEL_RE.match(base) or base == "localhost":
            continue
        names.add(base)
        names.add(base + ".local")
    result = frozenset(names)
    with _names_lock:
        _names_cache["own"] = (now, result)
    return result


def _device_names() -> frozenset:
    """The device's names for is_home_host (broader than own_loopback_names)."""
    now = time.monotonic()
    with _names_lock:
        hit = _names_cache.get("device")
        if hit is not None and now - hit[0] < _NAMES_TTL:
            return hit[1]
    names: set[str] = set()
    raw: list[str] = []
    try:
        from ..config import settings
        raw.append(getattr(settings, "device_hostname", "") or "")
    except Exception:
        pass
    try:
        raw.append(socket.gethostname())
    except Exception:
        pass
    for value in raw:
        name = host_name(value) if "://" in str(value) else _clean_name(value)
        if not name:
            continue
        names.add(name)
        if not name.endswith(".local"):
            names.add(name + ".local")
    result = frozenset(names)
    with _names_lock:
        _names_cache["device"] = (now, result)
    return result


def clear_name_cache() -> None:
    with _names_lock:
        _names_cache.clear()


_rejects_logged: set = set()


def _extra_networks() -> list:
    nets = list(lan.onlink_networks())
    try:
        from ..config import settings
        parsed, rejected = lan.parse_cidrs(settings.lan_cidrs)
    except Exception:
        parsed, rejected = [], []
    if rejected:
        key = tuple(rejected)
        if key not in _rejects_logged:
            _rejects_logged.add(key)
            log.warning("Ignoring home network entries that are not valid: %s",
                        ", ".join(rejected[:8]))
    return nets + parsed


def _peer(request) -> str | None:
    try:
        return request.client.host if request.client else None
    except Exception:
        return None


def gateway_peers() -> frozenset:
    """Docker's gateway addresses while this app runs on a Docker bridge
    network, where a reverse proxy on the Docker host connects from. Empty in
    the Home Assistant add-on, whose gateway is Home Assistant itself, and
    off Docker. Never raises."""
    try:
        if addon_runtime() or not lan.in_docker_bridge():
            return frozenset()
        return frozenset(lan.default_gateways())
    except Exception:
        return frozenset()


def _forager_tunnel_in_use() -> bool:
    try:
        from ..config import settings
        return getattr(settings, "tunnel_mode", "") == "forager"
    except Exception:
        return True


def classify_origin(request) -> str:
    """classify_origin_raw for a Starlette request (cached on request.state)."""
    try:
        cached = getattr(request.state, "pr_origin", None)
        if cached:
            return cached
    except Exception:
        cached = None
    try:
        origin = classify_origin_raw(
            _peer(request), request.headers, request.method,
            request.headers.get("host", ""), addon=addon_runtime(),
            extra_networks=_extra_networks(), own_names=own_loopback_names(),
            http_version=request.scope.get("http_version", "1.1"),
            gateway_peers=gateway_peers(), forager_tunnel=_forager_tunnel_in_use())
    except Exception:
        origin = "public"
    try:
        request.state.pr_origin = origin
    except Exception:
        pass
    return origin


def sign_in_from_outside(request) -> bool:
    """Whether a password sign-in on this request counts as from outside the
    home network, where a password alone is not enough.

    One answer for the sign-in page, the setup code and Settings, Security,
    so a refused sign-in always leads to a setup code page that takes the
    code. Home Assistant's Ingress, this device's own programs and the home
    network (the private ranges, the networks this device is attached to and
    Home networks) are home. A request through a proxy or tunnel, or one that
    names this kitchen's remote-access address, is outside. So is a peer on a
    public address, except the shared range 100.64.0.0/10 that Tailscale and
    carrier-grade NAT use, which signed in with a password alone before 0.21
    and still does. When unsure, outside."""
    try:
        origin = classify_origin(request)
        if origin == "ingress":
            return False
        if origin == "proxied":
            return True
        from ..config import settings
        from .request_origin import client_is_public, is_internet_request
        peer = _peer(request)
        if is_internet_request(request.headers.get("host", ""), settings.qr_public_url,
                               settings.tunnel_enabled, peer, False):
            return True
        if origin in ("lan_direct", "plain_loopback"):
            return False
        ip_text = unwrap_peer(peer)
        if ip_text is None:
            return True
        return client_is_public(ip_text)
    except Exception:
        return True


def same_host_proxy_signal(request) -> str:
    """The same-host proxy signal, only for the shape such a proxy leaves.

    The peer is 127.0.0.1 or ::1 (or, on a Docker bridge network, Docker's
    gateway), there is no forwarding header, and the Host is a local one; a
    foreign Host is DNS rebinding or a proxy that keeps Host, which is
    already treated as proxied and is not what the owner's fix addresses."""
    try:
        peer = unwrap_peer(_peer(request))
        if peer not in _LOOPBACK_PEERS and peer not in gateway_peers():
            return ""
        if has_forwarding_header(request.headers):
            return ""
        own = own_loopback_names()
        if not _is_local_name(host_name(request.headers.get("host", "")), own):
            return ""
        return same_host_proxy_signal_raw(
            request.headers, request.scope.get("http_version", "1.1"), own)
    except Exception:
        return ""


def is_ingress(request) -> bool:
    return classify_origin(request) == "ingress"


def sighting_host(request) -> str:
    """A safe label for where an open-mode sighting came from.

    The Host header when it is a plain host name or IP literal, else 'a proxy'
    for a proxied request and the peer address for any other."""
    try:
        raw = (request.headers.get("host", "") or "").strip().lower()
        name = host_name(raw)
        if name:
            try:
                ipaddress.ip_address(name)
                return name
            except ValueError:
                if lan.valid_host_name(name):
                    return name
        if has_forwarding_header(request.headers):
            return "a proxy"
        return unwrap_peer(_peer(request)) or "a proxy"
    except Exception:
        return "a proxy"


def first_forwarded_ip(request) -> str:
    """The first X-Forwarded-For entry when it is an IP, else ''."""
    try:
        first = (request.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
        return unwrap_peer(first) or ""
    except Exception:
        return ""


def is_home_host(host, *, device_names=None, extra_names=None) -> bool:
    """Whether a Host is one of this home's names.

    True for no Host, an IP literal, localhost, a single label, a name under a
    home-only suffix, the device's own names, or an owner-listed name."""
    name = host_name(host)
    if not name:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name:
        return True
    if any(name.endswith(s) for s in HOME_SUFFIXES):
        return True
    if device_names is None:
        device_names = _device_names()
    if name in device_names:
        return True
    if extra_names is None:
        try:
            from ..config import settings
            extra_names = lan.parse_host_names(settings.home_host_names)[0]
        except Exception:
            extra_names = []
    return name in extra_names


def origin_header_ok(headers, host) -> bool:
    """True without Origin; False for 'null'; else Origin and Host must match."""
    if not _has(headers, "origin"):
        return True
    origin = _get(headers, "origin").strip()
    if not origin or origin.lower() == "null":
        return False
    try:
        parts = urlsplit(origin)
        o_host = (parts.hostname or "").lower()
        default = 443 if parts.scheme == "https" else 80
        o_port = parts.port or default
        h = urlsplit("//" + str(host or "").strip().lower())
        h_host = (h.hostname or "").rstrip(".")
        h_port = h.port or default
    except ValueError:
        return False
    return bool(o_host) and o_host.rstrip(".") == h_host and o_port == h_port


def parse_client(headers) -> str:
    value = _get(headers, "x-pr-client").strip()
    return value if _CLIENT_RE.match(value) else ""


def parse_caps(headers) -> frozenset:
    raw = _get(headers, "x-pr-caps")
    caps: list[str] = []
    for token in raw.split(","):
        token = token.strip().lower()
        if _CAP_RE.match(token) and token not in caps:
            caps.append(token)
        if len(caps) >= 16:
            break
    return frozenset(caps)


# --- Principal ---------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    kind: str                       # session|key|agent|kiosk|legacy_loopback|anonymous
    scope: str | None               # admin|viewer|kiosk|legacy|satellite|ha|ha_rest|display|deck|reader|None
    credential_id: str | None = None
    client: str = ""
    caps: frozenset = field(default_factory=frozenset)
    origin: str = "public"
    bad_key: bool = False


def ANONYMOUS(origin: str = "public", **kw) -> Principal:  # noqa: N802 - contract name
    return Principal(kind="anonymous", scope=None, origin=origin, **kw)


def principal(request) -> Principal:
    """The request's principal, set by the auth middleware."""
    try:
        p = getattr(request.state, "principal", None)
        if isinstance(p, Principal):
            return p
    except Exception:
        pass
    return ANONYMOUS("public")


# --- Route families and scopes -----------------------------------------------


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


_PUBLIC_EXACT = frozenset({"/health", "/ui/login", "/", "/manifest.webmanifest", "/sw.js"})
_ADMIN_TREES = ("/setup", "/admin", "/tunnel")
_ADMIN_EXACT_ANY = frozenset({"/api/pairing/approve", "/api/pairing/deny",
                              "/api/pairing/pending", "/docs", "/redoc",
                              "/openapi.json", "/docs/oauth2-redirect"})
_ADMIN_POSTS = frozenset({"/printing/install", "/printing/bluetooth/setup",
                          "/printing/add", "/printing/remove", "/gadgets/install"})
_HA_FAMILY = {("GET", "/ha/state"), ("POST", "/ha/settings"), ("POST", "/ha/display"),
              ("POST", "/ha/connect"), ("POST", "/ha/disconnect"),
              ("POST", "/ha/revoke-key")}
_EVENTS_FAMILY = {("POST", "/events/notify"), ("POST", "/events/camera-popup"),
                  ("POST", "/events/camera-detect"), ("POST", "/events/navigate")}
_READER_FAMILY = {("GET", "/gadgets/config"), ("GET", "/gadgets/outputs"),
                  ("POST", "/gadgets/readings")}
_DISPLAY_FAMILY = {("GET", "/cub/summary"), ("POST", "/cub/ble-adv")}

VIEWER_EXCLUDED = frozenset({"/ha/connect", "/ha/disconnect", "/ha/revoke-key"})
KIOSK_DEVICE_ROUTES = frozenset({
    ("GET", "/setup/system/health"), ("GET", "/setup/hardware/status"),
    ("GET", "/setup/kiosk/activity"), ("POST", "/setup/kiosk/activity"),
    ("POST", "/setup/kiosk/screensaver"), ("GET", "/setup/kiosk/navigate/pending"),
    ("GET", "/setup/calibrate/touch/page"), ("GET", "/setup/calibrate/touch/pending"),
    ("GET", "/setup/calibrate/touch/cancel/pending"),
    ("GET", "/setup/calibrate/touch/done/pending"),
    ("GET", "/admin/update-notice"), ("GET", "/admin/version"),
})


def _norm_method(method) -> str:
    m = str(method or "GET").upper()
    return "GET" if m == "HEAD" else m


def route_family(method, path) -> str:
    """public, admin, ha, events, reader, display, fleet or kitchen."""
    m = _norm_method(method)
    p = str(path or "")
    if p.startswith("/static/") or p in _PUBLIC_EXACT:
        return "public"
    if p == "/api/pairing/request" or p.startswith("/api/pairing/status/"):
        return "public"
    if m == "GET" and p == "/ui/kiosk/launch":
        return "public"
    if m == "POST" and p == "/ui/kiosk/relaunch":
        return "public"
    if m in ("GET", "POST") and p == "/ui/setup-code":
        return "public"
    if any(_under(p, t) for t in _ADMIN_TREES):
        return "admin"
    if _under(p, "/api/credentials"):
        if m == "POST" and p == "/api/credentials/exchange":
            return "kitchen"
        return "admin"
    if p in _ADMIN_EXACT_ANY:
        return "admin"
    if m in ("POST", "DELETE") and _under(p, "/api/devices"):
        return "admin"
    if m == "GET" and p == "/ui/camera/preview":
        return "admin"
    if m == "POST" and p in _ADMIN_POSTS:
        return "admin"
    key = (m, p)
    if key in _HA_FAMILY:
        return "ha"
    if key in _EVENTS_FAMILY:
        return "events"
    if key in _READER_FAMILY:
        return "reader"
    if key in _DISPLAY_FAMILY:
        return "display"
    if m == "GET" and p == "/api/config/satellite":
        return "fleet"
    if _under(p, "/api/proxy") or _under(p, "/api/fleet"):
        return "fleet"
    return "kitchen"


_SEG = r"[^/]+"
_TIMER_ID_RE = re.compile(rf"^/timers/{_SEG}$")
_TIMER_EXTEND_RE = re.compile(rf"^/timers/{_SEG}/extend$")

_HA_REST_POSTS = frozenset({"/pending/scan", "/pending/scanner-mode",
                            "/pending/scanner-mode/cycle", "/timers",
                            "/gadgets/esp-action"})
_DISPLAY_ROUTES = frozenset({("GET", "/cub/summary"), ("POST", "/cub/ble-adv"),
                             ("POST", "/gadgets/esp-action"), ("GET", "/gadgets/esp-screen")})
_READER_ROUTES = frozenset({
    ("GET", "/gadgets/config"), ("GET", "/gadgets/outputs"), ("GET", "/cub/summary"),
    ("POST", "/gadgets/readings"), ("POST", "/gadgets/key-action"),
    ("POST", "/pending/scanner-mode"), ("POST", "/pending/scan"),
})


def _timer_writes(m: str, p: str) -> bool:
    return ((m == "DELETE" and (_TIMER_ID_RE.match(p) is not None))
            or (m == "POST" and _TIMER_EXTEND_RE.match(p) is not None))


def allows_raw(scope, method, path, *, kiosk_is_admin) -> bool:
    """Whether a scope may make this request (default deny)."""
    m = _norm_method(method)
    p = str(path or "")
    fam = route_family(m, p)
    if scope == "admin":
        return True
    if scope in ("viewer", "satellite"):
        return fam != "admin" and p not in VIEWER_EXCLUDED
    if scope == "legacy":
        return fam != "admin"
    if scope == "kiosk":
        if kiosk_is_admin:
            return True
        if (m, p) in KIOSK_DEVICE_ROUTES:
            return True
        return fam != "admin" and p not in VIEWER_EXCLUDED
    if scope == "ha":
        return fam in ("ha", "events")
    if scope == "ha_rest":
        if fam == "events":
            return True
        if m == "GET" and p == "/ha/state":
            return True
        if m == "GET" and fam == "kitchen":
            return True
        if m == "POST" and p in _HA_REST_POSTS:
            return True
        if m == "DELETE" and p == "/timers":
            return True
        return _timer_writes(m, p)
    if scope == "display":
        if (m, p) in _DISPLAY_ROUTES:
            return True
        if m == "GET" and p.startswith("/cub/firmware/"):
            return True
        return _timer_writes(m, p)
    if scope == "deck":
        return fam == "kitchen" or p.startswith("/api/proxy/ha/")
    if scope == "reader":
        return (m, p) in _READER_ROUTES
    return False


# --- Per-request snapshot ----------------------------------------------------


@dataclass(frozen=True)
class AccessSnapshot:
    password_set: bool = False
    configured: bool = True
    is_satellite: bool = False
    demo_mode: bool = False
    kiosk_is_admin: bool = True
    legacy_trust_active: bool = True
    grace_active: bool = False
    grace_until: str | None = None
    required: frozenset = frozenset()
    server_has_password: bool | None = None
    camera_relay_active: bool = False
    upstream_linked: bool = False


def allows(p: Principal, method, path, snap: AccessSnapshot) -> bool:
    if p is None or p.kind == "anonymous" or not p.scope:
        return False
    return allows_raw(p.scope, method, path, kiosk_is_admin=snap.kiosk_is_admin)


def effective_required_families(settings, auth_state_mod) -> dict:
    """Which keyless families need a key on an install with no password.

    The owner's saved switch wins. Otherwise a fresh install requires keys and
    an upgraded one keeps them open; on a satellite Home Assistant and its
    notifications stay open, because Home Assistant reaches a satellite with
    the main server's key, which a satellite cannot check yet."""
    saved = getattr(settings, "require_key_families", None)
    saved = saved if isinstance(saved, dict) else {}
    try:
        fresh = auth_state_mod.origin() == "fresh"
    except Exception:
        fresh = False
    try:
        sat = bool(settings.is_satellite())
    except Exception:
        sat = False
    out = {}
    for fam in FAMILIES:
        if isinstance(saved.get(fam), bool):
            out[fam] = saved[fam]
        elif sat and fam in ("ha", "events"):
            out[fam] = False
        else:
            out[fam] = fresh
    return out


def legacy_trust_applies() -> bool:
    """Whether the old trust for this device's programs (legacy loopback)
    holds right now.

    It holds while the owner's Settings choice to trust them again lasts, or,
    for the trust an upgrade carried over, only on a device that runs programs
    of its own (local_agents.own_programs_possible). On a server without them
    a request from loopback with no key is treated like any other visitor,
    because a reverse proxy on the same machine left on its defaults (nginx
    from 1.30) cannot be told apart from a local program."""
    from . import auth_state, local_agents
    try:
        if not auth_state.legacy_trust_active():
            return False
        if auth_state.retrust_active():
            return True
        return local_agents.own_programs_possible()
    except Exception:
        return False


def snapshot(request=None) -> AccessSnapshot:
    """The settings and state the access rules read, once per request."""
    if request is not None:
        try:
            cached = getattr(request.state, "pr_snapshot", None)
            if isinstance(cached, AccessSnapshot):
                return cached
        except Exception:
            pass
    from ..config import settings
    from . import auth_state
    try:
        relay = settings.fleet_relay if settings.is_satellite() else {}
        shp = relay.get("server_has_password") if isinstance(relay, dict) else None
        shp = shp if isinstance(shp, bool) else None
    except Exception:
        shp = None
    try:
        configured = bool(settings.is_configured())
    except Exception:
        configured = True
    req = effective_required_families(settings, auth_state)
    snap = AccessSnapshot(
        password_set=bool(settings.auth_password),
        configured=configured,
        is_satellite=bool(settings.is_satellite()),
        demo_mode=bool(getattr(settings, "demo_mode", False)),
        kiosk_is_admin=bool(getattr(settings, "kiosk_screen_is_admin", True)),
        legacy_trust_active=legacy_trust_applies(),
        grace_active=auth_state.grace_active(),
        grace_until=auth_state.grace_until(),
        required=frozenset(f for f, on in req.items() if on),
        server_has_password=shp,
        camera_relay_active=bool(settings.fleet_relay_active("camera")),
        upstream_linked=bool(settings.upstream_linked()),
    )
    if request is not None:
        try:
            request.state.pr_snapshot = snap
        except Exception:
            pass
    return snap


# --- Decisions ---------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    action: str            # serve|serve_grace|redirect_login|redirect_home|refuse|setup_code_page|open_refused_page|name_refused_page
    status: int = 200
    detail: str = ""
    code: str = ""
    notes: tuple = ()
    target: str = ""


def _serve(*notes) -> Decision:
    return Decision("serve", 200, notes=tuple(n for n in notes if n))


def _refuse(status, detail, code="", *notes) -> Decision:
    return Decision("refuse", status, detail, code, tuple(n for n in notes if n))


def _anon_open(origin, method, path, family, snap, host_ok, origin_ok, gated) -> Decision:
    """Open mode, nobody signed in."""
    m = _norm_method(method)
    if family in FAMILIES:
        if origin == "ingress":
            return _refuse(401, "Unauthorized")
        if family in snap.required and origin in ("lan_direct", "plain_loopback"):
            return _refuse(401, "Unauthorized", "", "keyless")
    if origin in OUTSIDE_ORIGINS:
        if snap.demo_mode:
            return _serve()
        if snap.grace_active:
            return Decision("serve_grace", 200, notes=("sighting:" + origin,))
        return Decision("open_refused_page", 403, OPEN_REFUSAL_MSG, "open_mode_refused",
                        ("setup_code:open",))
    notes = ["keyless"] if origin in ("lan_direct", "plain_loopback") else []
    if origin == "lan_direct":
        if m not in SAFE_METHODS and not origin_ok:
            return _refuse(403, CROSS_SITE_MSG, "cross_site")
        notes.append("gateway_check")
        if not host_ok:
            if snap.grace_active:
                return Decision("serve_grace", 200, notes=tuple(notes + ["sighting:name"]))
            return Decision("name_refused_page", 403, NAME_REFUSAL_MSG,
                            "open_mode_unknown_name", tuple(notes))
    if gated:
        return _refuse(403, SATELLITE_GATE_MSG, "", *(notes + ["relaunch"]))
    return _serve(*notes)


# Setup-bypass paths still open to a caller with no sign-in once a password
# exists on an install that is not set up yet. Each only reports progress to
# the kitchen screen (the setup QR code, the first-boot readiness page and its
# poll, the "setup just finished" flag) and changes nothing.
SETUP_STATUS_PATHS = frozenset({
    "/ui/qr", "/ui/getting-ready", "/ui/getting-ready/status",
    "/setup/kiosk/navigate/pending",
})


def decide(p: Principal, method, path, family, snap: AccessSnapshot, *,
           host_ok=True, origin_ok=True, gated=False, setup_bypass=False) -> Decision:
    """The access decision for one request (require_auth is a thin shell)."""
    m = _norm_method(method)
    origin = p.origin
    if family == "public":
        return _serve()
    if family == "fleet":
        if p.kind == "anonymous" or p.bad_key:
            return _serve()
        if allows(p, m, path, snap):
            return _serve()
        return _refuse(403, SCOPE_MSG)
    anonymous = p.kind == "anonymous"
    if not snap.configured:
        if anonymous and origin in OUTSIDE_ORIGINS:
            return Decision("setup_code_page", 403, SETUP_CODE_MSG, "setup_code_required",
                            ("setup_code:unconfigured",))
        # The wizard is open to anyone only while there is no password to ask
        # for. With one set (a container template's AUTH_PASSWORD, say), the
        # setup pages need a sign-in like every other admin page, so a
        # stranger on the network cannot finish setup, turn the password off
        # or start two-factor on the owner's behalf.
        if setup_bypass and (not snap.password_set
                             or (path in SETUP_STATUS_PATHS
                                 and m in ("GET", "HEAD"))):
            return _serve()
        if setup_bypass and anonymous and path == "/setup" and m == "GET":
            return Decision("redirect_login", 303, target="/ui/login")
    if not snap.password_set:
        if not anonymous:
            if allows(p, m, path, snap):
                if p.kind == "legacy_loopback":
                    anon = _anon_open(origin, m, path, family, snap, host_ok, origin_ok, gated)
                    if anon.action != "serve":
                        return _serve("legacy_use")
                return _serve()
            if origin in HOME_ORIGINS:
                if origin == "plain_loopback" and snap.legacy_trust_active:
                    return _serve("legacy_use")
                return _anon_open(origin, m, path, family, snap, host_ok, origin_ok, gated)
            if family == "admin" and p.kind in ("key", "agent"):
                return _refuse(403, ADMIN_KEY_MSG)
            if p.scope in ("viewer", "kiosk"):
                return _refuse(403, VIEWER_MSG)
            return _refuse(403, SCOPE_MSG)
        return _anon_open(origin, m, path, family, snap, host_ok, origin_ok, gated)
    # Password mode.
    if anonymous:
        notes = ["relaunch"]
        if origin in ("lan_direct", "plain_loopback"):
            notes.append("keyless")
        if str(path or "").startswith("/ui"):
            return Decision("redirect_login", 303, notes=tuple(notes), target="/ui/login")
        return _refuse(401, "Unauthorized", "", *notes)
    if allows(p, m, path, snap):
        if p.kind == "legacy_loopback":
            return _serve("legacy_use")
        return _serve()
    if (origin == "plain_loopback" and snap.legacy_trust_active
            and p.kind in ("key", "agent")):
        return _serve("legacy_use", "key_admin_loopback" if family == "admin" else "")
    admin_like = family == "admin" or str(path or "") in VIEWER_EXCLUDED
    if p.scope == "viewer" and admin_like:
        if m == "GET" and path == "/setup":
            return Decision("redirect_home", 303, target="/ui/")
        return _refuse(403, VIEWER_MSG)
    if p.scope == "kiosk" and admin_like:
        if m == "GET" and path == "/setup":
            return Decision("redirect_home", 303, target="/ui/?kiosk=1")
        return _refuse(403, VIEWER_MSG)
    if family == "admin" and p.kind in ("key", "agent"):
        return _refuse(403, ADMIN_KEY_MSG, "", "key_admin_refused")
    return _refuse(403, SCOPE_MSG)


# --- Keyless devices and the first password ----------------------------------


def raise_item(kind: str, title: str, body: str, dedupe: str, level: str = "info") -> None:
    """Create (or refresh) one inbox item. Never raises."""
    try:
        from ..database import SessionLocal
        from . import action_items
        db = SessionLocal()
        try:
            action_items.create(db, kind, title, body=body, dedupe_key=dedupe, level=level)
        finally:
            db.close()
    except Exception:
        log.debug("Could not raise the %s item", kind, exc_info=True)


_raised_once: dict = {}
_once_lock = threading.Lock()


def raise_item_once(kind: str, title: str, body: str, dedupe: str, level: str = "info") -> None:
    """raise_item at most once per dedupe key in this process.

    For items a request can raise (a key refused on Settings, a key used on
    Settings from this device): a script that repeats the call every few
    seconds then costs one database write a day, not one per request."""
    with _once_lock:
        if dedupe in _raised_once:
            return
        if len(_raised_once) >= 512:
            _raised_once.clear()
        _raised_once[dedupe] = True
    raise_item(kind, title, body, dedupe, level)


def clear_raised_once() -> None:
    with _once_lock:
        _raised_once.clear()


def labels_text(labels) -> str:
    labels = list(labels or [])
    if len(labels) <= 1:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def note_password_state_and_notify(now=None) -> bool:
    """Notice the moment a first password appears; warn about keyless devices.

    Returns True when this call raised the keyless_devices_need_key item."""
    try:
        from ..config import settings
        from . import auth_state
        if not auth_state.note_password_state(bool(settings.auth_password), now=now):
            return False
        labels = auth_state.keyless_device_labels(30, now=now)
        if not labels:
            return False
        day = datetime.now().date().isoformat()
        raise_item("keyless_devices_need_key", KEYLESS_ITEM_TITLE,
                   KEYLESS_ITEM_BODY.format(labels=labels_text(labels)),
                   f"keyless_devices_need_key:{day}", level="warning")
        return True
    except Exception:
        log.debug("keyless device check failed", exc_info=True)
        return False


def home_host_ok(request) -> bool:
    """is_home_host for this request, skipped for recovery-hotspot clients."""
    try:
        peer = unwrap_peer(_peer(request))
        if peer:
            ip = ipaddress.ip_address(peer)
            if ip.version == 4 and ip in lan.RECOVERY_HOTSPOT_NET and lan.recovery_hotspot_up():
                return True
        return is_home_host(request.headers.get("host", ""))
    except Exception:
        return True


def banner_context(request) -> dict:
    """What the access banner shows for this request. Never raises."""
    out = {"open_notice": False, "grace_until": None, "grace_address": "",
           "grace_kind": None, "gateway_peer": False}
    try:
        from . import auth_state
        snap = snapshot(request)
        origin = classify_origin(request)
        open_mode = not snap.password_set
        out["open_notice"] = open_mode and origin != "ingress" and not snap.demo_mode
        this_kind = None
        if open_mode:
            if origin in OUTSIDE_ORIGINS:
                this_kind = origin
            elif origin == "lan_direct" and not home_host_ok(request):
                this_kind = "name"
        if open_mode and snap.grace_active:
            seen, other = auth_state.open_sightings()
            if seen or other or this_kind:
                out["grace_until"] = snap.grace_until
                if this_kind:
                    out["grace_address"] = sighting_host(request)
                    out["grace_kind"] = this_kind
                elif other or any(s.get("kind") in ("proxied", "public") for s in seen):
                    out["grace_kind"] = "proxied"
                else:
                    out["grace_kind"] = "name"
        out["gateway_peer"] = bool(open_mode and auth_state.gateway_peer_recent())
    except Exception:
        log.debug("banner_context failed", exc_info=True)
    return out
