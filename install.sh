#!/usr/bin/env bash
# Pantry Raider on-device installer (loader)
# ==========================================
# Run this ON the device (a freshly imaged Raspberry Pi, or any Debian/Ubuntu
# box) over SSH. It asks what you want to install, then provisions only that.
#
#   curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
#
# There is nothing to edit on your PC and no repo to clone on your PC. Flash the
# card with Raspberry Pi Imager (set wifi/hostname/locale there), boot, SSH in,
# and run the line above.
#
# What it does:
#   1. Detects whether this is a Raspberry Pi, and whether a display and/or a
#      Stream Deck are attached right now.
#   2. Asks for the deployment mode and which add-ons to enable.
#   3. Fetches the newest Pantry Raider release to the device, checks it
#      against the Pantry Raider release key (nothing is installed when the
#      check fails), and runs the provisioner from it
#      (scripts/image-build/firstboot.sh) with the choices you made.
#
# On a device that is already set up, it asks nothing and only adds the
# update helper from the checked release (set FORCE=1 to run the full install).
#
# Modes:
#   pi_hosted  - full stack on this Pi (Pantry Raider + Grocy, optional Mealie).
#   pi_remote  - thin client: NO Docker/Grocy here, just a kiosk and/or Stream
#                Deck pointed at a Pantry Raider server elsewhere on the LAN.
#   server     - full stack on a general (non-Pi) Debian/Ubuntu host.
#
# Non-interactive use (CI, scripted installs): set NONINTERACTIVE=1 and pass the
# choices as env vars (DEPLOYMENT_MODE, REMOTE_SERVER_URL, ENABLE_MEALIE,
# ENABLE_OLLAMA, ENABLE_KIOSK, ENABLE_STREAMDECK, ENABLE_PRINTING,
# DISPLAY_ROTATION, PR_HOSTNAME, ENABLE_FB_SPLASH, ENABLE_BOOT_SPLASH). Label printing is OFF by default (ENABLE_PRINTING
# is only turned on when you ask for it, so a normal install never installs CUPS
# or Bluetooth).
# UPDATE_CHANNEL=main installs and follows every change as soon as it is
# published; leave it unset to install and follow tested releases.
# PLAN_ONLY=1 prints the resolved plan, then exits without fetching, using
# sudo, or provisioning (used by the test suite).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Syracuse3DPrintingOrg/PantryRaider.git}"
# Where the repo is checked out ON THE DEVICE (never on the user's PC).
REPO_DIR="${REPO_DIR:-/opt/foodassistant-src}"
# Which code to install: stable (the newest tested release, the default) or
# main (every change as soon as it is published). Only "main" means main.
# Whatever is chosen, the download is checked against the Pantry Raider
# release key before any of it runs.
UPDATE_CHANNEL="${UPDATE_CHANNEL:-}"
# The channel a device that is already set up follows (kept by
# foodassistant-update). Used when UPDATE_CHANNEL is not set, so re-running
# the installer never moves a device off its channel.
CHANNEL_FILE="${CHANNEL_FILE:-/etc/foodassistant/update-channel}"
# resolve_channel [sudo]: set CHANNEL from UPDATE_CHANNEL, else CHANNEL_FILE.
# Called again once root is available, since the file may be root-only.
resolve_channel() {
  local saved=""
  if [ -z "$UPDATE_CHANNEL" ] && [ -f "$CHANNEL_FILE" ]; then
    saved="$(${1:-} head -n 1 "$CHANNEL_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  case "${UPDATE_CHANNEL:-$saved}" in main) CHANNEL="main" ;; *) CHANNEL="stable" ;; esac
}
resolve_channel
# The release-key list this device trusts (installed from the copy below when
# the device has none yet).
TRUST_FILE="${TRUST_FILE:-/etc/foodassistant/trust/release-signers}"
# Test hook: a signers file that replaces the embedded list below.
FA_BOOTSTRAP_SIGNERS="${FA_BOOTSTRAP_SIGNERS:-}"
# The provisioner's marker for a device that is already set up (the same
# default firstboot.sh uses).
DONE_MARKER="${DONE_MARKER:-/var/lib/foodassistant/firstboot.done}"

NONINTERACTIVE="${NONINTERACTIVE:-0}"
PLAN_ONLY="${PLAN_ONLY:-0}"
FORCE="${FORCE:-0}"

# The Pantry Raider release-key list the download is checked against when
# this device has none installed yet (the text of scripts/release-signers).
IFS= read -r -d '' EMBEDDED_SIGNERS <<'SIGNERS' || true
pantryraider-release namespaces="git,pantryraider-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHPWzaAihzRx7Y9JlXotaZfO0drDTOT/lXxvoePF82rd
SIGNERS

# The release verifier, run as: python3 -c "$PYVERIFY" <subcommand> <args>.
IFS= read -r -d '' PYVERIFY <<'PYVERIFY' || true
# Release verifier for Pantry Raider updates, standard library only. The
# canonical copy lives in foodassistant-update; firstboot.sh and install.sh
# carry byte-identical copies. Every subcommand prints one JSON line
# {ok, reason, ...} and exits 0. inspect-tag and inspect-commit skip the
# signature and serve only sudo foodassistant-update --allow-unsigned-once.
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

PRINCIPAL = "pantryraider-release"
IMAGE_NAME = "ghcr.io/syracuse3dprintingorg/pantryraider"
SIG_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
KEY_TYPE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+|sk-\S+)$")
VERSION_LINE = re.compile(r"""^APP_VERSION\s*(?::\s*\w+\s*)?=\s*["']([0-9]+\.[0-9]+\.[0-9]+)["']""", re.M)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FETCH_LIMIT = 4 * 1024 * 1024


def done(ok, reason="", **extra):
    out = {"ok": ok, "reason": reason}
    out.update(extra)
    print(json.dumps(out, sort_keys=True))
    sys.exit(0)


def git(gitdir, *args):
    r = subprocess.run(["git", "-C", gitdir, *args], capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout


def ssh_verify(signers, namespace, payload, sig):
    with tempfile.TemporaryDirectory() as tmp:
        sigfile = os.path.join(tmp, "payload.sig")
        with open(sigfile, "wb") as fh:
            fh.write(sig)
        try:
            r = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", signers, "-I", PRINCIPAL,
                 "-n", namespace, "-s", sigfile],
                input=payload, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False
    return r.returncode == 0


def parse_version(text):
    m = VERSION_LINE.search(text)
    return m.group(1) if m else ""


def tree_version(gitdir, commit):
    raw = git(gitdir, "show", commit + ":service/app/config.py")
    if raw is None:
        return ""
    return parse_version(raw.decode("utf-8", "replace"))


def vtuple(value):
    v = value.strip()
    if v.startswith("v"):
        v = v[1:]
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def headers(raw):
    head = raw.split(b"\n\n", 1)[0]
    out = {}
    for line in head.split(b"\n"):
        if line.startswith(b" ") or b" " not in line:
            continue
        key, _, value = line.partition(b" ")
        out.setdefault(key.decode("utf-8", "replace"), value.decode("utf-8", "replace"))
    return out


def verify_tag(gitdir, tag, signers=None):
    ref = "refs/tags/" + tag
    kind = git(gitdir, "cat-file", "-t", ref)
    if kind is None:
        done(False, "missing", tag=tag)
    kind = kind.strip()
    if kind == b"commit" and signers is None:
        commit = git(gitdir, "rev-parse", ref).decode().strip()
        version = tree_version(gitdir, commit)
        if not version or "v" + version != tag:
            done(False, "version_mismatch", tag=tag, commit=commit, version=version)
        done(True, "", tag=tag, commit=commit, version=version)
    if kind != b"tag":
        done(False, "not_annotated", tag=tag)
    raw = git(gitdir, "cat-file", "tag", ref)
    if raw is None:
        done(False, "missing", tag=tag)
    lines = raw.splitlines(keepends=True)
    begin = -1
    for i, line in enumerate(lines):
        if line.rstrip(b"\n") == SIG_BEGIN:
            begin = i
    if begin < 0:
        if signers is not None:
            done(False, "unsigned", tag=tag)
        begin = len(lines)
    payload = b"".join(lines[:begin])
    if payload and not payload.endswith(b"\n"):
        payload += b"\n"
    if signers is not None:
        sig = b"".join(lines[begin:])
        if not sig.endswith(b"\n"):
            sig += b"\n"
        if not ssh_verify(signers, "git", payload, sig):
            done(False, "bad_signature", tag=tag)
    head = headers(payload)
    if head.get("tag", "") != tag:
        done(False, "name_mismatch", tag=tag)
    if head.get("type", "") != "commit":
        done(False, "not_commit", tag=tag)
    commit = head.get("object", "")
    version = tree_version(gitdir, commit)
    if not version or "v" + version != tag:
        done(False, "version_mismatch", tag=tag, commit=commit, version=version)
    done(True, "", tag=tag, commit=commit, version=version)


def verify_commit(gitdir, rev, signers=None):
    full = git(gitdir, "rev-parse", "--verify", "-q", rev + "^{commit}")
    if full is None:
        done(False, "missing")
    commit = full.decode().strip()
    if signers is not None:
        raw = git(gitdir, "cat-file", "commit", commit)
        if raw is None:
            done(False, "missing", commit=commit)
        head, sep, body = raw.partition(b"\n\n")
        kept = []
        sig = []
        in_sig = False
        for line in head.split(b"\n"):
            if in_sig and line.startswith(b" "):
                sig.append(line[1:])
                continue
            in_sig = False
            if line.startswith(b"gpgsig "):
                in_sig = True
                sig.append(line[len(b"gpgsig "):])
                continue
            kept.append(line)
        if not sig:
            done(False, "unsigned", commit=commit)
        payload = b"\n".join(kept) + sep + body
        if not ssh_verify(signers, "git", payload, b"\n".join(sig) + b"\n"):
            done(False, "bad_signature", commit=commit)
    version = tree_version(gitdir, commit)
    if not version:
        done(signers is None, "no_version", commit=commit, version="")
    done(True, "", commit=commit, version=version)


def verify_file(path, sigfile, signers, namespace):
    try:
        with open(path, "rb") as fh:
            payload = fh.read()
        with open(sigfile, "rb") as fh:
            sig = fh.read()
    except OSError:
        done(False, "missing")
    if not ssh_verify(signers, namespace, payload, sig):
        done(False, "bad_signature")
    done(True, "")


def fingerprints(signers):
    try:
        with open(signers, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        done(False, "missing")
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        for i, tok in enumerate(tokens[:-1]):
            if KEY_TYPE.match(tok):
                try:
                    blob = base64.b64decode(tokens[i + 1], validate=True)
                except ValueError:
                    break
                digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
                found.append("SHA256:" + digest.rstrip("="))
                break
    if not found:
        done(False, "no_keys")
    done(True, "", fingerprints=",".join(found))


def vcmp(a, b):
    ta, tb = vtuple(a), vtuple(b)
    if ta is None or tb is None:
        done(False, "bad_version")
    done(True, "", result=(ta > tb) - (ta < tb))


def fetch(url, dest):
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read(FETCH_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        done(False, "missing" if exc.code == 404 else "http_" + str(exc.code))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if isinstance(getattr(exc, "reason", None), FileNotFoundError):
            done(False, "missing")
        if isinstance(exc, FileNotFoundError):
            done(False, "missing")
        done(False, "unreachable")
    if len(data) > FETCH_LIMIT:
        done(False, "too_large")
    tmp = dest + ".part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError:
        done(False, "write_failed")
    done(True, "", bytes=len(data))


def check_manifest(path, version, commit):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        done(False, "unreadable")
    if not isinstance(doc, dict):
        done(False, "unreadable")
    image = doc.get("image")
    if not isinstance(image, dict):
        image = {}
    if doc.get("format") != 1:
        done(False, "format")
    if doc.get("version") != version:
        done(False, "version")
    if doc.get("tag") != "v" + version:
        done(False, "tag")
    if doc.get("commit") != commit:
        done(False, "commit")
    if image.get("name") != IMAGE_NAME:
        done(False, "image_name")
    digest = image.get("index_digest")
    if not isinstance(digest, str) or not DIGEST.match(digest):
        done(False, "digest")
    done(True, "", digest=digest)


def main(argv):
    cmds = {
        "verify-tag": (verify_tag, 3),
        "verify-commit": (verify_commit, 3),
        "verify-file": (verify_file, 4),
        "fingerprints": (fingerprints, 1),
        "vcmp": (vcmp, 2),
        "fetch": (fetch, 2),
        "check-manifest": (check_manifest, 3),
        "inspect-tag": (verify_tag, 2),
        "inspect-commit": (verify_commit, 2),
    }
    if not argv or argv[0] not in cmds or len(argv) - 1 != cmds[argv[0]][1]:
        done(False, "usage")
    func = cmds[argv[0]][0]
    func(*argv[1:])


main(sys.argv[1:])
PYVERIFY

# -- pretty output ------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_CYAN=$'\033[1;36m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi
say()  { printf '%s==>%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
hr()   { printf '%s----------------------------------------%s\n' "$C_DIM" "$C_OFF"; }

# -- interactive helpers (read from the terminal, not stdin) ------------------
# When invoked as `curl ... | bash`, stdin is the script itself, so prompts must
# read from /dev/tty. If there is no terminal and we are interactive, that is a
# fatal setup error with a clear fix.
TTY="/dev/tty"
have_tty() { [ -e "$TTY" ] && { : >/dev/null 2>&1 <"$TTY"; }; }

prompt_line() {  # prompt default -> echoes the answer (or default if blank)
  local prompt="$1" def="${2:-}" ans=""
  printf '%s%s%s ' "$C_CYAN" "$prompt" "$C_OFF" >"$TTY"
  IFS= read -r ans <"$TTY" || ans=""
  printf '%s' "${ans:-$def}"
}

prompt_yn() {  # prompt default(y|n) -> returns 0 for yes, 1 for no
  local prompt="$1" def="$2" hint ans
  case "$def" in y|Y) hint="[Y/n]";; *) hint="[y/N]";; esac
  while :; do
    printf '%s%s %s%s ' "$C_CYAN" "$prompt" "$hint" "$C_OFF" >"$TTY"
    IFS= read -r ans <"$TTY" || ans=""
    ans="${ans:-$def}"
    case "$ans" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO)   return 1 ;;
      *) printf '  Please answer y or n.\n' >"$TTY" ;;
    esac
  done
}

prompt_choice() {  # title; then "key:label" pairs in $@ ; echoes chosen key
  local title="$1"; shift
  local -a keys=() labels=()
  local pair
  for pair in "$@"; do
    keys+=("${pair%%:*}"); labels+=("${pair#*:}")
  done
  local i
  printf '%s%s%s\n' "$C_CYAN" "$title" "$C_OFF" >"$TTY"
  for i in "${!keys[@]}"; do
    printf '  %s) %s\n' "$((i+1))" "${labels[$i]}" >"$TTY"
  done
  while :; do
    local sel
    printf '%sChoose 1-%s [1]:%s ' "$C_CYAN" "${#keys[@]}" "$C_OFF" >"$TTY"
    IFS= read -r sel <"$TTY" || sel=""
    sel="${sel:-1}"
    if [[ "$sel" =~ ^[0-9]+$ ]] && [ "$sel" -ge 1 ] && [ "$sel" -le "${#keys[@]}" ]; then
      printf '%s' "${keys[$((sel-1))]}"; return 0
    fi
    printf '  Enter a number between 1 and %s.\n' "${#keys[@]}" >"$TTY"
  done
}

# -- hardware detection (same signals firstboot.sh uses) ----------------------
is_raspberry_pi() {
  [ -n "${FORCE_PI:-}" ] && return 0
  local f
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$f" ] && tr -d '\0' <"$f" | grep -qi 'raspberry pi' && return 0
  done
  return 1
}
has_display() {
  [ -n "${FORCE_DISPLAY:-}" ] && return 0
  [ -e /dev/dri/card0 ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
  return 1
}
has_streamdeck() {
  [ -n "${FORCE_STREAMDECK:-}" ] && return 0
  if command -v lsusb >/dev/null 2>&1; then
    lsusb 2>/dev/null | grep -qi '0fd9:' && return 0
  fi
  grep -qil '0fd9' /sys/bus/usb/devices/*/idVendor 2>/dev/null && return 0
  return 1
}
board_model() {
  local f
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$f" ] && { tr -d '\0' <"$f"; return; }
  done
  echo "unknown"
}

yesno() { case "$1" in true|TRUE|1|yes|on) echo true;; *) echo false;; esac; }

# -- gather configuration -----------------------------------------------------
IS_PI=false; is_raspberry_pi && IS_PI=true
HAS_DISPLAY=false; has_display && HAS_DISPLAY=true
HAS_DECK=false; has_streamdeck && HAS_DECK=true

DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-}"
REMOTE_SERVER_URL="${REMOTE_SERVER_URL:-}"
# Mealie is OFF by default everywhere: recipes, the meal plan, and the
# shopping list are built into Pantry Raider. It remains available as an
# opt-in for people who already use Mealie: set ENABLE_MEALIE=true in the
# environment (or enable the with-mealie compose profile later).
ENABLE_MEALIE="${ENABLE_MEALIE:-false}"
ENABLE_OLLAMA="${ENABLE_OLLAMA:-false}"
ENABLE_KIOSK="${ENABLE_KIOSK:-}"
ENABLE_STREAMDECK="${ENABLE_STREAMDECK:-}"
# Label printing (CUPS + Bluetooth + generic drivers) is off by default: it is a
# separate opt-in the user makes here or later from the setup UI. An explicit
# ENABLE_PRINTING in the environment (1/true to turn it on) always wins.
ENABLE_PRINTING="${ENABLE_PRINTING:-}"
DISPLAY_ROTATION="${DISPLAY_ROTATION:-0}"
# Default mDNS name is the brand short name, so the box answers at pr.local.
# The name is read from PR_HOSTNAME (FA_HOSTNAME is accepted too), never from
# HOSTNAME: bash auto-populates $HOSTNAME with the running machine's name, so a
# HOSTNAME default here can never fire and every curl-pipe-bash install would
# inherit raspberrypi instead. The provisioner appends -2, -3, ... if the name
# is already taken on the network. HOSTNAME_REQUESTED holds only an explicit
# choice: it is the only name handed to the provisioner, so a server (which
# keeps its own name unless asked) is never renamed to the brand default.
HOSTNAME_REQUESTED="${PR_HOSTNAME:-${FA_HOSTNAME:-}}"
HOSTNAME_CHOICE="${HOSTNAME_REQUESTED:-pr}"

# 32-bit detection, with the same test hooks firstboot.sh honors.
detect_arch() { printf '%s' "${FORCE_ARCH:-$(uname -m)}"; }
detect_long_bit() { printf '%s' "${FORCE_LONG_BIT:-$(getconf LONG_BIT 2>/dev/null || echo 64)}"; }
is_32bit_os() {
  case "$(detect_arch)" in
    armv6l|armv7l|armhf|i386|i686) return 0 ;;
  esac
  [ "$(detect_long_bit)" = "32" ]
}

banner() {
  hr
  printf '%s  Pantry Raider installer%s\n' "$C_GREEN" "$C_OFF"
  hr
  if [ "$IS_PI" = true ]; then
    say "Device: $(board_model)"
  else
    say "Device: non-Pi host ($(uname -m))"
  fi
  say "Display attached:    $([ "$HAS_DISPLAY" = true ] && echo yes || echo no)"
  say "Stream Deck attached: $([ "$HAS_DECK" = true ] && echo yes || echo no)"
  hr
}

interactive_config() {
  have_tty || die "No terminal for questions. Run the installer from an SSH session, or give your choices up front: curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | NONINTERACTIVE=1 DEPLOYMENT_MODE=pi_hosted bash (use DEPLOYMENT_MODE=server on a machine that is not a Pi). Every choice is listed at https://docs.pantryraider.app/hardware/sd-image/#non-interactive-scripted-installs"

  # Mode is the only question asked interactively. Everything else (kiosk,
  # Stream Deck, display rotation, Ollama) is auto-detected or set later via
  # the web setup wizard at /setup. Mealie is not installed by default:
  # recipes, the meal plan, and shopping are built in. Set ENABLE_MEALIE=true
  # in the environment if you already use Mealie and want it alongside.
  if [ "$IS_PI" = true ]; then
    # Three clear modes. Kiosk and standalone are both the full local stack; the
    # difference is whether this device also drives a screen. A Stream Deck is
    # auto-detected in the kiosk modes (installed only when one is attached, so a
    # deckless kiosk carries no idle controller). Printing is asked separately
    # below (FoodAssistant-9mu5).
    local pick
    pick="$(prompt_choice "How will this device be used?" \
      "kiosk:Pi Host Kiosk      - full stack on this Pi WITH a touchscreen/kiosk (and Stream Deck if attached)" \
      "standalone:Pi Host Standalone - full stack on this Pi, headless (no kiosk/Stream Deck; add them later from the web UI)" \
      "remote:Pi Remote          - a kiosk/Stream Deck for a Pantry Raider server elsewhere on your network")"
    case "$pick" in
      kiosk)
        DEPLOYMENT_MODE="pi_hosted"
        [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK=true
        [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"
        ;;
      standalone)
        DEPLOYMENT_MODE="pi_hosted"
        [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK=false
        [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK=false
        ;;
      remote)
        DEPLOYMENT_MODE="pi_remote"
        [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK=true
        [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"
        ;;
    esac
  else
    say "Non-Pi host detected; using Server hosted mode."
    DEPLOYMENT_MODE="server"
  fi

  # In Pi Remote mode the server URL is NOT asked here. The device boots
  # with a small web setup app on port 80, so the end user (or you, before
  # shipping) sets the server URL from a browser at the device address. Set
  # REMOTE_SERVER_URL in the environment to pre-seed it for a hands-off image.
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ] && [ -z "$REMOTE_SERVER_URL" ]; then
    say "Server URL will be set later in the web setup wizard (no SSH needed)."
  fi

  # Auto-detect kiosk and Stream Deck based on attached hardware.
  # Display orientation, Mealie, and Ollama are configured in the web UI.
  [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK="$(default_kiosk)"
  [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"

  # Label printing: opt-in, off by default. Ask once here; it can also be turned
  # on later from Settings > Printing without reinstalling. Skip the question if
  # an explicit ENABLE_PRINTING was passed in the environment.
  if [ -z "$ENABLE_PRINTING" ]; then
    if prompt_yn "Enable label printing (installs CUPS + Bluetooth)?" n; then
      ENABLE_PRINTING=true
    else
      ENABLE_PRINTING=false
    fi
  fi
}

# The kiosk default when none was chosen. A Pi turns it on when a display is
# attached. A server never does on its own: most mini PCs have a graphics
# chip (so a display "exists" with no screen plugged in), and the kiosk takes
# over the machine's console. ENABLE_KIOSK=true still turns it on.
default_kiosk() {
  if [ "$DEPLOYMENT_MODE" = "server" ]; then
    echo false
  elif [ "$HAS_DISPLAY" = true ]; then
    echo true
  else
    echo false
  fi
}

# Non-interactive: fill any unset enable flags from detection so a bare
# NONINTERACTIVE=1 still does something sensible.
noninteractive_config() {
  [ -z "$DEPLOYMENT_MODE" ] && { [ "$IS_PI" = true ] && DEPLOYMENT_MODE="pi_hosted" || DEPLOYMENT_MODE="server"; }
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    ENABLE_MEALIE=false; ENABLE_OLLAMA=false
  fi
  [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK="$(default_kiosk)"
  [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"
  # Printing stays off unless the operator asked for it explicitly.
  if [ -z "$ENABLE_PRINTING" ]; then ENABLE_PRINTING=false; fi
}

# True when the install keeps this machine's current hostname: a server with
# no name asked for (PR_HOSTNAME or FA_HOSTNAME).
keeps_hostname() { [ "$DEPLOYMENT_MODE" = "server" ] && [ -z "$HOSTNAME_REQUESTED" ]; }

# The name the device answers at once installed, for the closing message.
final_hostname() {
  if keeps_hostname; then
    hostname 2>/dev/null || echo localhost
  else
    printf '%s\n' "$HOSTNAME_CHOICE"
  fi
}

# Warn early, before anything is installed, when the full stack is headed for
# a 32-bit OS: the Pantry Raider image is 64-bit only, so the stack would not
# start. The install continues (the kiosk and network steps still work), the
# same way the provisioner handles it.
warn_32bit() {
  [ "$DEPLOYMENT_MODE" = "pi_remote" ] && return 0
  is_32bit_os || return 0
  warn "32-bit OS detected ($(detect_arch)), and the Pantry Raider container image is 64-bit only."
  if [ "$IS_PI" = true ]; then
    warn "The stack will not start on this OS. Reflash with Raspberry Pi OS Lite (64-bit), then run this installer again."
    warn "Pi Remote (satellite) mode works on 32-bit, if this device is meant to control a server elsewhere."
  else
    warn "The stack will not start on this OS. Install a 64-bit version of Debian or Ubuntu, then run this installer again."
  fi
  warn "Continuing anyway."
}

confirm_plan() {
  hr
  say "Install plan"
  printf '  Mode:        %s\n' "$DEPLOYMENT_MODE"
  [ "$DEPLOYMENT_MODE" = "pi_remote" ] && printf '  Controls:    %s\n' "${REMOTE_SERVER_URL:-(set later in web wizard)}"
  if keeps_hostname; then
    printf '  Hostname:    %s (unchanged)\n' "$(final_hostname)"
  else
    printf '  Hostname:    %s\n' "$HOSTNAME_CHOICE"
  fi
  if [ "$DEPLOYMENT_MODE" != "pi_remote" ]; then
    printf '  Mealie:      %s\n' "$(yesno "$ENABLE_MEALIE")"
    printf '  Ollama:      %s\n' "$(yesno "$ENABLE_OLLAMA")"
  fi
  printf '  Kiosk:       %s%s\n' "$(yesno "$ENABLE_KIOSK")" \
    "$([ "$DISPLAY_ROTATION" != 0 ] && printf ' (rotated %s)' "$DISPLAY_ROTATION")"
  printf '  Stream Deck: %s\n' "$(yesno "$ENABLE_STREAMDECK")"
  printf '  Printing:    %s\n' "$(yesno "$ENABLE_PRINTING")"
  hr
}

# -- provisioning -------------------------------------------------------------
SUDO=""
need_root() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; return; fi
  command -v sudo >/dev/null 2>&1 || die "This step needs root. Re-run as root or install sudo."
  SUDO="sudo"
}

INSTALL_VERIFY_MSG="The Pantry Raider download could not be checked against the release key, so nothing was installed. Check the internet connection and run the installer again."

# Run the release verifier as root (the checkout is root's); prints its one
# JSON line.
pyv() { $SUDO python3 -c "$PYVERIFY" "$@" 2>/dev/null; }

# One field of a verifier JSON line: true/false print as 1/0, a missing key
# as "".
jget() {
  JSON_IN="$1" python3 -c '
import json, os, sys
try:
    d = json.loads(os.environ.get("JSON_IN", ""))
except ValueError:
    d = {}
v = d.get(sys.argv[1], "") if isinstance(d, dict) else ""
print("1" if v is True else "0" if v is False else v)' "$2" 2>/dev/null
}

# The key lines of a signers file (blank lines and comments dropped).
key_lines() {
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" 2>/dev/null \
    | grep -v -e '^$' -e '^#' || true
}

# git, ssh-keygen (which checks the signatures) and python3 (which runs the
# check) before any git work.
ensure_tools() {
  if command -v git >/dev/null 2>&1 && command -v ssh-keygen >/dev/null 2>&1 \
      && command -v python3 >/dev/null 2>&1; then
    return 0
  fi
  say "Installing git, openssh-client and python3"
  $SUDO apt-get update -y >/dev/null 2>&1 || true
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y git openssh-client python3 \
    || die "Could not install git, openssh-client and python3. Check the internet connection and run the installer again."
}

# The release-key list to check the download against: the one this device
# already trusts, else the one built into this installer.
SIGNERS=""
SIGNERS_TMP=""
pick_signers() {
  if [ -f "$TRUST_FILE" ] && [ -n "$(key_lines "$TRUST_FILE")" ]; then
    SIGNERS="$TRUST_FILE"
  elif [ -n "$FA_BOOTSTRAP_SIGNERS" ]; then
    SIGNERS="$FA_BOOTSTRAP_SIGNERS"
  else
    SIGNERS_TMP="$(mktemp)"
    printf '%s' "$EMBEDDED_SIGNERS" > "$SIGNERS_TMP"
    chmod 0644 "$SIGNERS_TMP"
    SIGNERS="$SIGNERS_TMP"
  fi
}
cleanup_signers() { [ -n "$SIGNERS_TMP" ] && rm -f "$SIGNERS_TMP"; return 0; }
trap cleanup_signers EXIT

# Clone or reuse REPO_DIR without trusting anything already in it, then check
# out the newest release signed by the Pantry Raider release key (or the
# signed tip of main on the main channel). Nothing is installed when nothing
# verifies.
VERIFIED_VERSION=""
fetch_repo() {
  say "Fetching Pantry Raider to $REPO_DIR (on this device)"
  ensure_tools
  pick_signers
  resolve_channel "$SUDO"
  if [ ! -d "$REPO_DIR/.git" ]; then
    { $SUDO mkdir -p "$REPO_DIR" && $SUDO git -C "$REPO_DIR" -c init.defaultBranch=main init -q; } \
      || die "Could not create $REPO_DIR."
  fi
  # An existing checkout keeps the remote it has.
  if ! $SUDO git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    $SUDO git -C "$REPO_DIR" remote add origin "$REPO_URL" || die "Could not set up $REPO_DIR."
  fi

  local commit="" version="" out="" names="" t reason
  if [ "$CHANNEL" = "main" ]; then
    say "Checking the newest change on the main channel"
    if $SUDO git -C "$REPO_DIR" fetch --depth 1 --no-tags origin \
        "+refs/heads/main:refs/remotes/origin/main" >/dev/null 2>&1; then
      out="$(pyv verify-commit "$REPO_DIR" refs/remotes/origin/main "$SIGNERS")"
      if [ "$(jget "$out" ok)" = "1" ]; then
        commit="$(jget "$out" commit)"
        version="$(jget "$out" version)"
      else
        reason="$(jget "$out" reason)"
        warn "The newest change on main is not signed by the Pantry Raider release key (${reason:-unknown})."
      fi
    else
      warn "Could not fetch main from $REPO_URL."
    fi
  else
    say "Checking the newest release"
    names="$($SUDO git -C "$REPO_DIR" ls-remote --tags --refs origin 'refs/tags/v*' 2>/dev/null \
      | awk '{print $2}' | sed 's|^refs/tags/||' \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -rV | head -n 20 || true)"
    [ -n "$names" ] || warn "No releases found at $REPO_URL."
    for t in $names; do
      if ! $SUDO git -C "$REPO_DIR" fetch --depth 1 --force --no-tags origin \
          "+refs/tags/$t:refs/tags/$t" >/dev/null 2>&1; then
        warn "$t could not be fetched; skipped."
        continue
      fi
      out="$(pyv verify-tag "$REPO_DIR" "$t" "$SIGNERS")"
      if [ "$(jget "$out" ok)" = "1" ]; then
        commit="$(jget "$out" commit)"
        version="$(jget "$out" version)"
        break
      fi
      reason="$(jget "$out" reason)"
      warn "$t did not pass the check (${reason:-unknown}); skipped."
    done
  fi
  [ -n "$commit" ] || die "$INSTALL_VERIFY_MSG"

  if [ "$CHANNEL" = "main" ]; then
    $SUDO git -C "$REPO_DIR" checkout -q --force -B main "$commit" || die "$INSTALL_VERIFY_MSG"
  else
    $SUDO git -C "$REPO_DIR" checkout -q --force --detach "$commit" || die "$INSTALL_VERIFY_MSG"
  fi
  VERIFIED_VERSION="$version"
  ok "Pantry Raider ${version:-} is ready at $REPO_DIR (checked against the release key)"
}

# Install the release-key list on this device when it has none yet, from the
# list the download was just checked against (never from the download).
install_trust() {
  if [ -f "$TRUST_FILE" ] && [ -n "$(key_lines "$TRUST_FILE")" ]; then
    return 0
  fi
  local dir
  dir="$(dirname "$TRUST_FILE")"
  { $SUDO mkdir -p "$dir" && $SUDO chmod 0755 "$dir" \
      && $SUDO install -m 0644 "$SIGNERS" "$TRUST_FILE"; } \
    || die "Could not install the release key at $TRUST_FILE."
  ok "Installed the Pantry Raider release key at $TRUST_FILE"
}

run_provisioner() {
  local fb="$REPO_DIR/scripts/image-build/firstboot.sh"
  [ -f "$fb" ] || die "Provisioner not found at $fb"
  say "Provisioning (this can take a few minutes on first run)"
  # Hand the provisioner a hostname only when one was chosen. Without one it
  # names a Pi "pr" (pr.local) and leaves a server's own name alone.
  local -a host_arg=()
  if [ -n "$HOSTNAME_REQUESTED" ]; then
    host_arg=("FA_HOSTNAME=$HOSTNAME_REQUESTED")
  fi
  $SUDO env \
    DEPLOYMENT_MODE="$DEPLOYMENT_MODE" \
    REMOTE_SERVER_URL="$REMOTE_SERVER_URL" \
    ENABLE_MEALIE="$(yesno "$ENABLE_MEALIE")" \
    ENABLE_OLLAMA="$(yesno "$ENABLE_OLLAMA")" \
    ENABLE_KIOSK="$(yesno "$ENABLE_KIOSK")" \
    ENABLE_STREAMDECK="$(yesno "$ENABLE_STREAMDECK")" \
    ENABLE_PRINTING="$(yesno "$ENABLE_PRINTING")" \
    ENABLE_FB_SPLASH="${ENABLE_FB_SPLASH:-}" \
    ENABLE_BOOT_SPLASH="${ENABLE_BOOT_SPLASH:-}" \
    DISPLAY_ROTATION="$DISPLAY_ROTATION" \
    ${host_arg[@]+"${host_arg[@]}"} \
    UPDATE_CHANNEL="$UPDATE_CHANNEL" \
    REPO_DIR="$REPO_DIR" \
    bash "$fb"
}

# A device that is already set up gets only the device helper and the update
# helper from the checked release (the provisioner's hostbridge step, which
# also installs every other host helper). Nothing else changes.
run_helper_install() {
  local fb="$REPO_DIR/scripts/image-build/firstboot.sh"
  [ -f "$fb" ] || die "Provisioner not found at $fb"
  say "Installing the update helper"
  $SUDO env \
    REPO_DIR="$REPO_DIR" \
    UPDATE_CHANNEL="$UPDATE_CHANNEL" \
    STEPS=hostbridge \
    bash "$fb"
}

# Already set up (the provisioner's done marker exists): add the update
# helper and leave everything else as it is. FORCE=1 runs the full install.
already_set_up() {
  [ "$FORCE" != "1" ] && [ -f "$DONE_MARKER" ]
}

helper_only_main() {
  say "This device already runs Pantry Raider. The installer adds the update helper and leaves everything else as it is."
  if [ "$PLAN_ONLY" = "1" ]; then
    printf 'PLAN helper-only repo_dir=%s channel=%s\n' "$REPO_DIR" "$CHANNEL"
    exit 0
  fi
  need_root
  fetch_repo
  install_trust
  run_helper_install
  hr
  ok "Done. Update from Settings, Backups & Updates (Update now), or run sudo foodassistant-update."
  hr
}

# Optional print stack: only when the operator opted in. Runs the idempotent
# host print-setup helper (CUPS + Bluetooth + generic drivers), then recreates
# the app container so it picks up the CUPS_SERVER the helper wrote to the
# stack .env. A no-op for anyone who did not enable printing.
run_print_setup() {
  [ "$(yesno "$ENABLE_PRINTING")" = "true" ] || return 0
  local ps="$REPO_DIR/scripts/image-build/foodassistant-print-setup"
  if [ ! -f "$ps" ]; then
    warn "Print setup helper not found at $ps; skipping print stack install."
    return 0
  fi
  say "Installing the label print stack (CUPS + Bluetooth)"
  $SUDO env REPO_DIR="$REPO_DIR" bash "$ps" || warn "Print stack setup reported problems; printing may need attention in Settings > Printing."
  # Recreate the app container so env_file (CUPS_SERVER) takes effect. Only the
  # non-remote stacks run Docker; a pi_remote venv app reads local cupsd
  # directly and needs no recreate.
  if [ "$DEPLOYMENT_MODE" != "pi_remote" ] && command -v docker >/dev/null 2>&1; then
    for d in /opt/foodassistant "$REPO_DIR"; do
      if [ -f "$d/docker-compose.yml" ]; then
        say "Applying print settings to the running app"
        # REPO_DIR tells the compose file where the checkout lives (it
        # mounts the Grocy start-up hook from there).
        ( cd "$d" && $SUDO env REPO_DIR="$REPO_DIR" docker compose up -d ) || warn "Could not recreate the app container; run 'docker compose up -d' in $d."
        break
      fi
    done
  fi
}

print_done() {
  hr
  ok "Pantry Raider installed."
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ] && [ -n "$REMOTE_SERVER_URL" ]; then
    say "This device controls: $REMOTE_SERVER_URL"
    say "To change it later, open the setup wizard in your browser:"
    printf '    %shttp://%s.local/setup%s\n' "$C_GREEN" "$HOSTNAME_CHOICE" "$C_OFF"
  elif [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    say "Open this URL in your browser to point the device at your server:"
    printf '    %shttp://%s.local/setup%s\n' "$C_GREEN" "$HOSTNAME_CHOICE" "$C_OFF"
    say "(If .local doesn't resolve, use the device IP instead.)"
  else
    say "Open this URL in your browser to finish configuration:"
    printf '    %shttp://%s.local:9284/setup%s\n' "$C_GREEN" "$(final_hostname)" "$C_OFF"
    say "(If .local doesn't resolve, use the device IP instead.)"
  fi
  hr
}

main() {
  banner
  if already_set_up; then
    helper_only_main
    return 0
  fi
  if [ "$NONINTERACTIVE" = "1" ]; then
    noninteractive_config
  else
    interactive_config
  fi
  # Pi Remote never runs Mealie/Ollama regardless of how flags arrived.
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    ENABLE_MEALIE=false; ENABLE_OLLAMA=false
  fi
  warn_32bit
  confirm_plan

  if [ "$PLAN_ONLY" = "1" ]; then
    # Emit a stable, greppable plan for tests/automation, then stop.
    # hostname=keep means this machine keeps the name it already has.
    local plan_host="$HOSTNAME_CHOICE"
    keeps_hostname && plan_host="keep"
    printf 'PLAN mode=%s remote=%s mealie=%s ollama=%s kiosk=%s streamdeck=%s printing=%s rotation=%s hostname=%s repo_dir=%s channel=%s\n' \
      "$DEPLOYMENT_MODE" "$REMOTE_SERVER_URL" \
      "$(yesno "$ENABLE_MEALIE")" "$(yesno "$ENABLE_OLLAMA")" \
      "$(yesno "$ENABLE_KIOSK")" "$(yesno "$ENABLE_STREAMDECK")" \
      "$(yesno "$ENABLE_PRINTING")" \
      "$DISPLAY_ROTATION" "$plan_host" "$REPO_DIR" "$CHANNEL"
    exit 0
  fi

  if [ "$NONINTERACTIVE" != "1" ]; then
    prompt_yn "Proceed with this install?" y || die "Aborted by user."
  fi

  need_root
  fetch_repo
  install_trust
  run_provisioner
  run_print_setup
  print_done
}

main "$@"
