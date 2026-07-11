"""Admin utilities: backup download/restore, rclone remote push, system status."""
import asyncio
import io
import json
import logging
import shutil
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings, SECRET_SETTING_KEYS, APP_VERSION, GITHUB_REPO
from ..passwords import verify_secret

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


from ..version_compare import (  # noqa: E402
    normalize as _normalize, is_version_tag as _is_version_tag, is_newer as _is_newer,
)


@router.get("/version")
async def version():
    """Current running version (no network call)."""
    return {"version": APP_VERSION}


class _LoggingReq(BaseModel):
    enabled: bool


@router.get("/logging")
async def logging_status():
    """Whether debug logging is on and the size of the captured log."""
    from ..services import diagnostics
    path = diagnostics.log_path(settings.data_dir)
    size = path.stat().st_size if path.exists() else 0
    return {"enabled": bool(settings.debug_logging), "bytes": size}


@router.post("/logging")
async def set_logging(req: _LoggingReq):
    """Turn debug logging on or off, persist it, and apply it immediately."""
    from ..services import diagnostics
    settings.save({"debug_logging": req.enabled})  # persists and applies to the live object
    diagnostics.configure_file_logging(settings.data_dir, req.enabled)
    logger.info("Debug logging %s via Settings.", "enabled" if req.enabled else "disabled")
    return {"ok": True, "enabled": req.enabled}


@router.get("/logs/download")
async def download_logs():
    """Stream the captured log (current plus rollovers) as a text download, with
    any configured secret values redacted so the bundle is safe to share."""
    from ..services import diagnostics
    secrets_values = [str(getattr(settings, k, "") or "") for k in SECRET_SETTING_KEYS]
    text = diagnostics.read_log_text(settings.data_dir, secrets_values)
    if not text:
        text = ("No log has been captured yet. Enable debug logging in Settings, "
                "reproduce the problem, then download again.\n")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        io.BytesIO(text.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="foodassistant-logs-{stamp}.txt"'},
    )


@router.get("/support-bundle")
async def support_bundle():
    """Stream a one-click support bundle zip (FoodAssistant-w7mb).

    App side: version manifest, redacted settings, the diagnostics log, state
    files, the last update-check result, and Python/package versions. On a Pi
    appliance the host bridge contributes a root-level report (unit states,
    journal tail, display and input probes); the bridge being down just leaves
    a note in the zip, it never fails the download. Everything in the zip is
    scrubbed of the configured secret values.
    """
    from ..hardware import is_raspberry_pi
    from ..services import support_bundle as sb

    host_sections = None
    if is_raspberry_pi():
        from ..services.bridge import bridge_client
        try:
            # The bridge shells out to systemctl and journalctl; give it time.
            # bridge_client carries the X-Bridge-Token so this keeps working once
            # the bridge stops accepting tokenless requests (FoodAssistant-ow4f).
            async with bridge_client(timeout=20.0) as c:
                r = await c.get("http://127.0.0.1:9299/support-bundle")
            if r.status_code == 200:
                data = r.json()
                sections = data.get("sections")
                if isinstance(sections, dict):
                    host_sections = {str(k): str(v) for k, v in sections.items()}
        except Exception as e:
            logger.info("Support bundle: host bridge unavailable (%s)", e)

    files = sb.build_bundle_files(settings, host_sections)
    zip_bytes = sb.build_zip_bytes(files)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="foodassistant-support-{stamp}.zip"'},
    )


@router.get("/check-update")
async def check_update():
    """Compare the running version with the latest on the configured channel.

    On the "main" channel: APP_VERSION is bumped on every commit but tagged only
    for minor/major releases, so the old tag-based check could
    never see a patch update and always reported "latest" (FoodAssistant-jhug).
    We instead read APP_VERSION straight from service/app/config.py on the main
    branch, which reflects every pushed patch.

    On the "stable" channel (FoodAssistant-wkwx): the newest published release
    is what the device would install, so the check asks the releases API for the
    latest release tag instead of reading main.

    Either primary falls back to the highest version tag if unavailable
    (offline, private repo, layout change, no releases yet). One or two outbound
    calls; returns gracefully on any error. The repo must be public for the
    unauthenticated calls to succeed.
    """
    import re
    import time
    import httpx

    def _record(latest: str, available: bool) -> None:
        # Remember when we last checked and what we found, so the UI can show a
        # "last checked" line without re-checking on every load (FoodAssistant-lq01).
        try:
            settings.save({"update_last_checked": time.time(),
                           "update_last_latest": latest,
                           "update_last_available": bool(available)})
        except Exception:
            pass
    # raw.githubusercontent.com is served through a CDN that caches each file for
    # a few minutes, so just after a push it can still return the previous
    # APP_VERSION and the check wrongly reports "up to date". A unique query
    # string busts that cache, and we also send no-cache headers, so a Check for
    # updates right after a release sees the new version (Pantry Raider).
    raw_url = (f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/service/app/config.py"
               f"?cb={int(time.time())}")
    _no_cache = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    stable = settings.update_channel == "stable"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            if stable:
                # Primary on the stable channel: the latest published release,
                # which is exactly what a stable-channel device would install.
                try:
                    rr = await client.get(
                        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                        headers={"Accept": "application/vnd.github+json"})
                    if rr.status_code == 200:
                        tag = str(rr.json().get("tag_name", "") or "")
                        if _is_version_tag(tag):
                            avail = _is_newer(tag, APP_VERSION)
                            _record(tag, avail)
                            return {"ok": True, "current": APP_VERSION, "latest": tag,
                                    "update_available": avail, "checked_at": time.time(),
                                    "release_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"}
                except Exception:
                    pass  # fall through to the tag check
            else:
                # Primary on main: the version on the branch tip (every patch
                # bump lands here).
                try:
                    rr = await client.get(raw_url, headers=_no_cache)
                    if rr.status_code == 200:
                        m = re.search(r'APP_VERSION\s*=\s*["\']([0-9][0-9.]*)["\']', rr.text)
                        if m:
                            latest = m.group(1)
                            avail = _is_newer(latest, APP_VERSION)
                            _record(latest, avail)
                            return {"ok": True, "current": APP_VERSION, "latest": latest,
                                    "update_available": avail, "checked_at": time.time(),
                                    "release_url": f"https://github.com/{GITHUB_REPO}"}
                except Exception:
                    pass  # fall through to the tag check
            # Fallback: the highest version tag (covers tagged releases).
            r = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/tags",
                headers={"Accept": "application/vnd.github+json"},
                params={"per_page": 100},
            )
        if r.status_code != 200:
            hint = " (private repo or no tags yet)" if r.status_code == 404 else ""
            return {"ok": False, "current": APP_VERSION,
                    "error": f"GitHub returned HTTP {r.status_code}{hint}."}
        tags = [t.get("name", "") for t in r.json() if _is_version_tag(t.get("name", ""))]
        if not tags:
            return {"ok": False, "current": APP_VERSION, "error": "No version tags found yet."}
        latest = max(tags, key=_normalize)  # tags API isn't semver-sorted; pick the highest
        avail = _is_newer(latest, APP_VERSION)
        _record(latest, avail)
        return {"ok": True, "current": APP_VERSION, "latest": latest,
                "update_available": avail, "checked_at": time.time(),
                "release_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{latest}"}
    except Exception as e:
        return {"ok": False, "current": APP_VERSION,
                "error": f"Could not reach GitHub ({e.__class__.__name__})."}


# How long a recorded update-check result is reused before the passive popup
# poller asks GitHub again. The Settings "Check for updates" button always does
# a live check; only the on-screen popup poller prefers this cache, so a fleet
# of open browsers (and the kiosk's frequent reloads) never hammer the API.
UPDATE_CACHE_TTL_SECONDS = 3 * 3600


def _release_url_for(latest: str) -> str:
    """The page the Update action opens on a device without in-app OTA.

    On the stable channel the newest published release is the target; on main
    the repo landing page is, since main installs track the branch rather than a
    tagged release. Mirrors the URLs check_update returns on its live paths.
    """
    if settings.update_channel == "stable":
        return f"https://github.com/{GITHUB_REPO}/releases/tag/{latest}"
    return f"https://github.com/{GITHUB_REPO}"


def should_notify_update(update_available: bool, latest: str, dismissed: str) -> bool:
    """Whether the on-screen "update available" popup should show (FoodAssistant-5wtc).

    True only when an update is available and this browser has not already
    dismissed that exact version, so the prompt pops once per version and a
    later, newer version pops again. Pure and side-effect free so it is unit
    tested on its own.
    """
    if not update_available or not latest:
        return False
    return _normalize(latest) != _normalize(dismissed or "")


@router.get("/update-notice")
async def update_notice(dismissed: str = "", prefer_cache: bool = True):
    """Passive update check that backs the on-screen popup (FoodAssistant-5wtc).

    Reuses check_update but prefers the last recorded result while it is recent
    (UPDATE_CACHE_TTL_SECONDS), so a normal page load, and especially the
    kiosk's frequent reloads, do not hit GitHub on every view. Returns whether
    the popup should show for this browser given the version it last dismissed,
    plus whether this device can apply an in-app OTA update (a Pi appliance) or
    should open the release page instead. Any check failure is a quiet
    show=False, so the popup simply does not appear.
    """
    import time

    cached = None
    if prefer_cache and settings.update_last_checked:
        age = time.time() - float(settings.update_last_checked or 0)
        if 0 <= age < UPDATE_CACHE_TTL_SECONDS and settings.update_last_latest:
            cached = {
                "ok": True,
                "current": APP_VERSION,
                "latest": settings.update_last_latest,
                "update_available": bool(settings.update_last_available),
                "checked_at": float(settings.update_last_checked),
                "release_url": _release_url_for(settings.update_last_latest),
            }

    result = cached if cached is not None else await check_update()
    is_pi = settings.is_pi_appliance()
    if not result.get("ok"):
        return {"ok": False, "show": False,
                "current": result.get("current", APP_VERSION),
                "is_pi_appliance": is_pi}
    latest = str(result.get("latest", ""))
    return {
        "ok": True,
        "show": should_notify_update(
            bool(result.get("update_available")), latest, dismissed),
        "current": result.get("current", APP_VERSION),
        "latest": latest,
        "update_available": bool(result.get("update_available")),
        "release_url": result.get("release_url", f"https://github.com/{GITHUB_REPO}"),
        "is_pi_appliance": is_pi,
        "checked_at": result.get("checked_at"),
    }


def _require_current_password(password: str, message: str) -> None:
    """Gate a sensitive admin action on a fresh re-entry of the app password.

    Shared by the backup download (FoodAssistant-16cj) and the restore, both of
    which a walk-up at an already-open Settings page could otherwise trigger. An
    open install (no auth_password configured) has nothing to verify against, so
    the action proceeds as before rather than locking anyone out. The compare is
    constant-time via verify_secret and the entered password is never logged.
    """
    if not settings.auth_password:
        return
    if not verify_secret(password, settings.auth_password):
        raise HTTPException(403, message)


def _require_backup_password(password: str) -> None:
    """Gate the settings backup on a fresh re-entry of the app password
    (FoodAssistant-16cj).

    The backup carries the SQLite database and, with include_secrets, the raw
    API keys and passwords, so a walk-up at an already-open Settings page could
    otherwise download every secret. We re-confirm the current password before
    streaming.
    """
    _require_current_password(
        password, "Enter your current password to download the backup.")


@router.post("/backup")
async def download_backup(backup_password: str = Form(""),
                          include_secrets: bool = Form(False)):
    """Stream a zip of all Pantry Raider app data as a browser download.

    Includes settings.json, the SQLite database, and any user-edited data
    files. By default API keys, passwords and the TOTP/session secrets are
    redacted from settings.json and rclone.conf is omitted, so the file is
    safe to store off-box. Pass include_secrets=true for a restore-complete
    backup (store it somewhere trusted). Grocy and Mealie data live in separate
    containers: use scripts/backup.sh on the host to capture everything.

    Because the download can carry secrets, the current app password must be
    re-entered (posted as backup_password); a wrong or missing password returns
    403 and streams nothing.
    """
    _require_backup_password(backup_password)
    zip_bytes, filename = _build_zip(include_secrets=include_secrets)
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# rclone.conf holds cloud-storage credentials, so it is treated like a secret.
_SECRET_FILES = {"rclone.conf"}


def _redact_settings(raw: bytes) -> bytes:
    """Blank out credential fields in a settings.json byte blob."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw  # not parseable: leave as-is rather than risk corrupting
    for k in SECRET_SETTING_KEYS:
        if k in data and data[k]:
            data[k] = ""
    return json.dumps(data, indent=2).encode()


def _build_zip(include_secrets: bool = False) -> tuple[bytes, str]:
    """Create the backup zip in memory, return (bytes, filename).

    When include_secrets is False (default), settings.json is redacted and
    files holding raw credentials (rclone.conf) are skipped.
    """
    data_dir = Path(settings.data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if data_dir.exists():
            for f in sorted(data_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(data_dir)
                arc_name = Path("foodassistant-data") / rel
                if not include_secrets and f.name in _SECRET_FILES:
                    continue
                if not include_secrets and f.name == "settings.json":
                    zf.writestr(str(arc_name), _redact_settings(f.read_bytes()))
                else:
                    zf.write(f, arc_name)
    suffix = "" if include_secrets else "-redacted"
    return buf.getvalue(), f"foodassistant-backup-{date.today()}{suffix}.zip"


# The top-level directory the backup zip nests everything under (see _build_zip).
_BACKUP_PREFIX = "foodassistant-data"


def _safe_members(zf: zipfile.ZipFile, data_dir: Path) -> list[tuple[str, Path]]:
    """Resolve archive members to their destinations under data_dir, safely.

    Returns (arcname, dest_path) pairs for the regular files that live under the
    expected "foodassistant-data/" prefix and stay inside data_dir once resolved.
    Anything else (directories, absolute paths, or "../" escapes that would write
    outside data_dir) is skipped, so a tampered archive cannot drop files
    elsewhere on the host (zip-slip).
    """
    base = data_dir.resolve()
    out: list[tuple[str, Path]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if not parts or parts[0] != _BACKUP_PREFIX:
            continue
        rel = "/".join(parts[1:])
        if not rel:
            continue
        dest = (base / rel).resolve()
        if dest != base and base not in dest.parents:
            continue  # path escapes data_dir; refuse it
        out.append((info.filename, dest))
    return out


def _restore_zip(zip_bytes: bytes) -> dict:
    """Restore app data from a backup zip produced by _build_zip.

    Validates the archive, snapshots the current data dir aside (so a bad restore
    is recoverable), extracts the members, then keeps any currently-stored secret
    that the backup left blank (a redacted backup blanks secrets, and we never
    want a restore to wipe a working credential). Finally it reloads the live
    settings and disposes the database engine so the restored DB is picked up.
    Returns a summary dict.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise HTTPException(400, "That file is not a valid zip backup.")

    data_dir = Path(settings.data_dir)
    members = _safe_members(zf, data_dir)
    if not members:
        raise HTTPException(
            400,
            "This zip does not look like a Pantry Raider backup "
            "(no 'foodassistant-data/' contents).",
        )

    # Snapshot the current data aside before overwriting anything.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot = data_dir.parent / f"{data_dir.name}.pre-restore-{stamp}"
    if data_dir.exists():
        shutil.copytree(data_dir, snapshot, dirs_exist_ok=True)

    # Remember the secrets we currently hold so a redacted backup cannot blank them.
    current_secrets = {k: getattr(settings, k, "") for k in SECRET_SETTING_KEYS}

    restored = 0
    for arcname, dest in members:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(arcname) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        restored += 1

    # Merge restored settings with the secret-preserve-on-blank rule, then apply.
    secrets_preserved = 0
    sf = data_dir / "settings.json"
    if sf.exists():
        try:
            loaded = json.loads(sf.read_text())
        except Exception:
            loaded = {}
        for k in SECRET_SETTING_KEYS:
            if not loaded.get(k) and current_secrets.get(k):
                loaded[k] = current_secrets[k]
                secrets_preserved += 1
        sf.write_text(json.dumps(loaded, indent=2))
        try:
            sf.chmod(0o600)
        except OSError:
            pass
        settings.apply(loaded)

    # Drop any open handle to the old defaults DB so the restored file is used.
    try:
        from ..database import engine
        engine.dispose()
    except Exception:
        logger.warning("Could not dispose DB engine after restore", exc_info=True)

    # Re-cache providers and Mealie data against the restored settings.
    try:
        from ..dependencies import reset_providers
        reset_providers()
    except Exception:
        pass
    try:
        from ..services.mealie import reset_cache, reset_staple_cache
        reset_cache()
        reset_staple_cache()
    except Exception:
        pass

    return {
        "ok": True,
        "restored_files": restored,
        "secrets_preserved": secrets_preserved,
        "snapshot": str(snapshot) if data_dir.exists() else "",
    }


@router.post("/restore")
async def restore_backup(file: UploadFile = File(...),
                         restore_password: str = Form("")):
    """Restore Pantry Raider app data from an uploaded backup zip.

    The counterpart to POST /admin/backup: it rewrites this app's data directory
    (settings.json, the defaults database, staples) from the archive. Grocy and
    Mealie data live in separate containers and are not touched here; use
    scripts/restore.sh on the host for a full snapshot. The current data dir is
    copied aside first, and a redacted backup keeps the secrets already stored.

    Because a restore overwrites settings and the database, the current app
    password must be re-entered (posted as restore_password), the same gate the
    backup download uses; a wrong or missing password returns 403 and changes
    nothing. Verified before the upload is read so a failed check does no work.
    """
    _require_current_password(
        restore_password, "Enter your current password to restore a backup.")
    zip_bytes = await file.read()
    if not zip_bytes:
        raise HTTPException(400, "No file was uploaded.")
    return _restore_zip(zip_bytes)


@router.post("/backup/remote")
async def push_to_remote(include_secrets: bool = False):
    """Write the backup zip to disk and push it to the configured rclone remote.

    Requires rclone to be installed in the container and a remote configured
    at the path set in RCLONE_REMOTE (Settings > Security > Backup). Secrets
    are redacted by default since the destination is third-party cloud storage.
    """
    if not settings.rclone_remote:
        raise HTTPException(400, "No rclone remote configured: set one in Settings > Security > Backup.")
    import shutil
    if not shutil.which("rclone"):
        raise HTTPException(500, "rclone is not installed in this container. Rebuild the image after adding it to the Dockerfile.")

    # Defense in depth (the save path already validates): never let a stored
    # remote reach the command line unless it has the safe remote:path shape,
    # and terminate flag parsing with "--" (rclone uses spf13/pflag, which
    # honours it) so a positional argument can never be read as a flag.
    from ..services.backup_remote import valid_remote
    if not valid_remote(settings.rclone_remote):
        raise HTTPException(400, "The backup remote must look like remote:path "
                                 "(or an absolute path). Fix it in Settings > Backup.")

    zip_bytes, filename = _build_zip(include_secrets=include_secrets)
    tmp = Path("/tmp") / filename
    tmp.write_bytes(zip_bytes)
    try:
        dest = settings.rclone_remote.strip().rstrip("/") + "/" + filename
        env = {"RCLONE_CONFIG": str(Path(settings.data_dir) / "rclone.conf")}
        proc = await asyncio.create_subprocess_exec(
            "rclone", "copyto", "--", str(tmp), dest,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**__import__('os').environ, **env},
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise HTTPException(502, f"rclone failed: {stderr.decode()[:400]}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "message": f"Backup pushed to {settings.rclone_remote}", "filename": filename}


@router.get("/backup/usb/status")
async def usb_backup_status():
    """Attached USB drive status for the Backup pane: whether a drive is
    mounted, its free space, and when the last backup was written. On a Pi
    appliance this asks the host bridge; on a server the app looks itself."""
    from ..services import usb_backup
    st = await usb_backup.status()
    st["interval_hours"] = settings.usb_backup_interval_hours
    st["last_run"] = settings.usb_backup_last
    return st


@router.post("/backup/usb")
async def usb_backup_now():
    """Write a backup to the attached USB drive now.

    On a Pi Hosted box this snapshots the whole stack (app data, Grocy, and
    Mealie when present); on a Pi Remote it saves the device config; on a
    server it writes the app-data zip. Backups land in a pantryraider-backups
    folder on the drive and the newest 14 are kept.
    """
    from ..services import usb_backup
    result = await usb_backup.run_backup()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "USB backup failed."))
    return result


@router.post("/backup/test-remote")
async def test_remote():
    """Test whether rclone can reach the configured remote."""
    if not settings.rclone_remote:
        return {"ok": False, "error": "No rclone remote configured."}
    import shutil
    if not shutil.which("rclone"):
        return {"ok": False, "error": "rclone not found in container. Rebuild image with rclone installed."}
    # Same boundary as push_to_remote: only a safe remote:path shape reaches
    # the command line, and "--" stops rclone from reading it as a flag.
    from ..services.backup_remote import valid_remote
    if not valid_remote(settings.rclone_remote):
        return {"ok": False, "error": "The backup remote must look like remote:path "
                                      "(or an absolute path)."}
    env = {"RCLONE_CONFIG": str(Path(settings.data_dir) / "rclone.conf")}
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "lsd", "--", settings.rclone_remote.strip(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**__import__('os').environ, **env},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            return {"ok": True, "message": f"Remote reachable: {settings.rclone_remote}"}
        return {"ok": False, "error": stderr.decode()[:300] or "Remote unreachable."}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timed out connecting to remote."}
    except Exception as e:
        return {"ok": False, "error": str(e)}
