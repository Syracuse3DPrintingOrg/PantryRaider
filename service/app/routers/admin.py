"""Admin utilities: backup download/restore, rclone remote push, system status."""
import asyncio
import io
import json
import logging
import shutil
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

from ..config import settings, SECRET_SETTING_KEYS, APP_VERSION, GITHUB_REPO

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


def _normalize(v: str) -> tuple:
    """Turn a version string like 'v1.2.3' into a comparable tuple (1, 2, 3)."""
    parts = v.lstrip("vV").split(".")
    out = []
    for p in parts:
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


@router.get("/version")
async def version():
    """Current running version (no network call)."""
    return {"version": APP_VERSION}


def _is_version_tag(name: str) -> bool:
    """Looks like a version tag, e.g. v1.0.0 or 1.2."""
    body = name.lstrip("vV")
    return bool(body) and body[0].isdigit()


@router.get("/check-update")
async def check_update():
    """Compare the running version with the highest version tag on GitHub.

    Uses the tags API, so a bare git tag is enough (no published Release
    required). Makes one outbound call; returns gracefully offline. The repo
    must be public for the unauthenticated call to succeed.
    """
    import httpx
    url = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url, headers={"Accept": "application/vnd.github+json"},
                                 params={"per_page": 100})
        if r.status_code != 200:
            hint = " (private repo or no tags yet)" if r.status_code == 404 else ""
            return {"ok": False, "current": APP_VERSION,
                    "error": f"GitHub returned HTTP {r.status_code}{hint}."}
        tags = [t.get("name", "") for t in r.json() if _is_version_tag(t.get("name", ""))]
        if not tags:
            return {"ok": False, "current": APP_VERSION, "error": "No version tags found yet."}
        latest = max(tags, key=_normalize)  # tags API isn't semver-sorted; pick the highest
        update = _normalize(latest) > _normalize(APP_VERSION)
        return {"ok": True, "current": APP_VERSION, "latest": latest,
                "update_available": update,
                "release_url": f"https://github.com/{GITHUB_REPO}/releases/tag/{latest}"}
    except Exception as e:
        return {"ok": False, "current": APP_VERSION,
                "error": f"Could not reach GitHub ({e.__class__.__name__})."}


@router.get("/backup")
async def download_backup(include_secrets: bool = False):
    """Stream a zip of all FoodAssistant app data as a browser download.

    Includes settings.json, the SQLite database, and any user-edited data
    files. By default API keys, passwords and the TOTP/session secrets are
    redacted from settings.json and rclone.conf is omitted, so the file is
    safe to store off-box. Pass include_secrets=true for a restore-complete
    backup (store it somewhere trusted). Grocy and Mealie data live in separate
    containers: use scripts/backup.sh on the host to capture everything.
    """
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
            "This zip does not look like a FoodAssistant backup "
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
async def restore_backup(file: UploadFile = File(...)):
    """Restore FoodAssistant app data from an uploaded backup zip.

    The counterpart to GET /admin/backup: it rewrites this app's data directory
    (settings.json, the defaults database, staples) from the archive. Grocy and
    Mealie data live in separate containers and are not touched here; use
    scripts/restore.sh on the host for a full snapshot. The current data dir is
    copied aside first, and a redacted backup keeps the secrets already stored.
    """
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

    zip_bytes, filename = _build_zip(include_secrets=include_secrets)
    tmp = Path("/tmp") / filename
    tmp.write_bytes(zip_bytes)
    try:
        dest = settings.rclone_remote.rstrip("/") + "/" + filename
        env = {"RCLONE_CONFIG": str(Path(settings.data_dir) / "rclone.conf")}
        proc = await asyncio.create_subprocess_exec(
            "rclone", "copyto", str(tmp), dest,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**__import__('os').environ, **env},
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise HTTPException(502, f"rclone failed: {stderr.decode()[:400]}")
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "message": f"Backup pushed to {settings.rclone_remote}", "filename": filename}


@router.post("/backup/test-remote")
async def test_remote():
    """Test whether rclone can reach the configured remote."""
    if not settings.rclone_remote:
        return {"ok": False, "error": "No rclone remote configured."}
    import shutil
    if not shutil.which("rclone"):
        return {"ok": False, "error": "rclone not found in container. Rebuild image with rclone installed."}
    env = {"RCLONE_CONFIG": str(Path(settings.data_dir) / "rclone.conf")}
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "lsd", settings.rclone_remote,
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
