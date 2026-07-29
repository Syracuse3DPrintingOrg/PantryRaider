"""Cloud backup storage for Premium kitchens (FoodAssistant-kzjz).

A paired install can push its data-directory zip here and pull it back,
so a kitchen's settings and local data survive a lost or replaced device
without the owner touching a file. The feature is Premium only: every
endpoint re-checks the account's entitlement server-side (usage.premium_active)
and refuses a trial or basic account with a clear message. The UI gate in the
app is convenience only; this is the real gate.

Storage design: the zip lives on the VPS filesystem under
settings.backup_storage_dir, in a per-account subdirectory, never in Postgres.
The database holds only a small CloudBackup row (account, instance, filename,
size, created_at). A per-file size cap and a per-account retention count keep
storage bounded. Every list and download is scoped by the token's account, so
one account can never reach another's backups (IDOR-safe).
"""
from __future__ import annotations

import os
import secrets as _secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import usage
from ..config import settings
from ..deps import current_instance, get_db, utc_now_iso
from ..models import CloudBackup, Instance

router = APIRouter(prefix="/v1/backup", tags=["backup"])

# The one message a non-Premium account gets from every backup endpoint, so the
# owner learns the feature exists and how to unlock it rather than guessing at a
# bare 403.
PREMIUM_REQUIRED_MESSAGE = (
    "Cloud backup is a Premium feature. Upgrade your Forager plan to back up "
    "this kitchen to the cloud.")


def _require_premium(db: Session, account_id: int) -> None:
    """Refuse the request unless the account holds an active Premium plan.

    Answered as 402 Payment Required (the same code the AI proxy uses for a
    plan gate), with the structured body the app surfaces as a clear upgrade
    nudge."""
    if not usage.premium_active(db, account_id):
        raise HTTPException(402, detail={
            "error": "premium_required",
            "message": PREMIUM_REQUIRED_MESSAGE,
        })


def _account_dir(account_id: int) -> Path:
    """The storage directory for one account, created on demand.

    account_id is an integer primary key, so it can never carry path
    separators; the directory name is always a plain number."""
    base = Path(settings.backup_storage_dir) / str(int(account_id))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _new_filename() -> str:
    """A server-generated, collision-proof zip name. The client's filename is
    never trusted for the path (that would be a traversal risk)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"backup-{stamp}-{_secrets.token_hex(4)}.zip"


def _enforce_retention(db: Session, account_id: int) -> None:
    """Keep only the newest N backups for the account; delete older files+rows.

    Ordered by id descending (monotonic with insertion), so the freshest
    uploads are kept even if two share a second-resolution timestamp."""
    keep = max(1, int(settings.backup_retention_count))
    rows = (db.query(CloudBackup)
            .filter_by(account_id=account_id)
            .order_by(CloudBackup.id.desc())
            .all())
    for row in rows[keep:]:
        path = _account_dir(account_id) / row.filename
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # a missing file must not block pruning the stale row
        db.delete(row)
    if len(rows) > keep:
        db.commit()


def _serialize(row: CloudBackup) -> dict:
    return {"id": row.id, "filename": row.filename,
            "size_bytes": row.size_bytes, "created_at": row.created_at}


@router.post("/upload")
async def upload_backup(file: UploadFile = File(...),
                        inst: Instance = Depends(current_instance),
                        db: Session = Depends(get_db)):
    """Store this kitchen's backup zip. Premium only, size-capped, retained.

    The account is re-checked for Premium here regardless of what the app's UI
    allowed, so the client can never talk its way past the gate. The upload is
    rejected if it exceeds settings.backup_max_bytes; on success the oldest
    backups past the retention count are evicted."""
    _require_premium(db, inst.account_id)
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="The backup file was empty.")
    if len(data) > settings.backup_max_bytes:
        cap_mb = settings.backup_max_bytes // 1_000_000
        raise HTTPException(413, detail={
            "error": "too_large",
            "message": (f"That backup is too large. The limit is about "
                        f"{cap_mb} MB."),
        })

    account_dir = _account_dir(inst.account_id)
    filename = _new_filename()
    dest = account_dir / filename
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)  # atomic: a partial write is never a listed backup

    row = CloudBackup(account_id=inst.account_id, instance_id=inst.id,
                      filename=filename, size_bytes=len(data),
                      created_at=utc_now_iso())
    db.add(row)
    db.commit()
    db.refresh(row)
    _enforce_retention(db, inst.account_id)
    return _serialize(row)


@router.get("/list")
def list_backups(inst: Instance = Depends(current_instance),
                 db: Session = Depends(get_db)):
    """The account's own backups, newest first. Scoped to the token's account,
    so it can never enumerate another account's backups."""
    _require_premium(db, inst.account_id)
    rows = (db.query(CloudBackup)
            .filter_by(account_id=inst.account_id)
            .order_by(CloudBackup.id.desc())
            .all())
    return {"backups": [_serialize(r) for r in rows]}


def _owned_backup(db: Session, account_id: int, backup_id: int) -> CloudBackup:
    """Look up a backup and confirm it belongs to this account.

    A backup owned by another account answers the same 404 as one that does not
    exist, so a probing caller learns nothing (IDOR-safe)."""
    row = db.get(CloudBackup, backup_id)
    if row is None or row.account_id != account_id:
        raise HTTPException(404, detail="No such backup.")
    return row


def _stream(account_id: int, row: CloudBackup) -> FileResponse:
    path = _account_dir(account_id) / row.filename
    if not path.exists():
        # The row survived but its file did not (manual cleanup, disk loss). Be
        # honest rather than stream an empty body.
        raise HTTPException(410, detail="That backup is no longer on the server.")
    return FileResponse(path, media_type="application/zip",
                        filename=row.filename)


@router.get("/latest")
def download_latest(inst: Instance = Depends(current_instance),
                    db: Session = Depends(get_db)):
    """Stream the account's newest backup. What the app's one-click restore
    pulls when the owner does not pick a specific one."""
    _require_premium(db, inst.account_id)
    row = (db.query(CloudBackup)
           .filter_by(account_id=inst.account_id)
           .order_by(CloudBackup.id.desc())
           .first())
    if row is None:
        raise HTTPException(404, detail="No backups stored yet.")
    return _stream(inst.account_id, row)


@router.get("/download/{backup_id}")
def download_backup(backup_id: int,
                    inst: Instance = Depends(current_instance),
                    db: Session = Depends(get_db)):
    """Stream one of the account's own backups by id (never another's)."""
    _require_premium(db, inst.account_id)
    row = _owned_backup(db, inst.account_id, backup_id)
    return _stream(inst.account_id, row)


@router.delete("/{backup_id}")
def delete_backup(backup_id: int,
                  inst: Instance = Depends(current_instance),
                  db: Session = Depends(get_db)):
    """Delete one of the account's own backups (file and row)."""
    _require_premium(db, inst.account_id)
    row = _owned_backup(db, inst.account_id, backup_id)
    path = _account_dir(inst.account_id) / row.filename
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": backup_id}
