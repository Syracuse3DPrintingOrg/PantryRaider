"""The remote-access tunnel API.

A kitchen enables remote access here: it sends its WireGuard public key and a
hostname hint, the cloud allocates a stable tunnel IP and a subdomain, tells
the VPS agent to add the WireGuard peer and the Caddy route, and hands back
everything the app needs to bring up its side of the tunnel. Remote access is
a paid or trial feature, so enable is gated on an active entitlement.

The private key never leaves the device; the cloud only ever sees the public
half. Nothing is persisted until the agent confirms the peer, so a failed
agent call leaves no orphan row.

Every path that deletes a kitchen goes through teardown_instance_tunnel
first, so its peer and route leave the VPS with it. When the agent cannot be
reached the removal is recorded as a TunnelTeardown and retried by the
periodic sweep, and that address and name stay out of allocation until the
VPS has really let go of them.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import tunnel as alloc
from .. import ratelimit, tunnel_client, usage
from ..config import settings
from ..deps import current_instance, get_db, utc_now_iso
from ..models import Instance, TunnelPeer, TunnelTeardown

router = APIRouter(prefix="/v1/tunnel", tags=["tunnel"])

# Enable reads the addresses and names in use, waits on the VPS agent, then
# writes its choice. Two kitchens enabling at the same moment could both pick
# the same free address or name, and the agent replaces a peer that holds
# either, so one kitchen's web address could end up serving another account's
# kitchen. Forager runs one worker process (see docs), so a process-wide lock
# makes allocation and programming the VPS one step.
_ENABLE_LOCK = threading.Lock()


def _apex_domain() -> str:
    """The domain kitchen subdomains hang off, derived from the public base
    URL (e.g. "forager.pantryraider.app")."""
    base = settings.public_base_url
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return host.strip().lower()


def _dns_name(subdomain: str) -> str:
    return f"{subdomain}.{_apex_domain()}"


def _public_url(subdomain: str) -> str:
    return f"https://{_dns_name(subdomain)}"


class EnableRequest(BaseModel):
    public_key: str
    hostname_hint: str = ""
    # An explicit web address the kitchen chose. When set it is sanitized and
    # used as the subdomain (uniqueness-checked; a taken name is a 409 with a
    # free suggestion). Left blank, the subdomain is derived from
    # hostname_hint, the long-standing behavior.
    subdomain: str = ""
    # The port the kitchen's app listens on behind the tunnel. A Pi appliance
    # publishes on the host at 9284 (the default); a plain server runs
    # WireGuard inside the app container and is reached on its internal 8000.
    # Older installs omit it, so it defaults to 9284 and existing peers keep
    # working unchanged.
    app_port: int | None = 9284


def _entitled(db: Session, account_id: int) -> bool:
    """Whether the account may use remote access right now: any active
    entitlement, paid, comped, or trial (the same usage.has_active_access
    the AI proxy and the app's status endpoint read)."""
    state = usage.quota_state(db, account_id, usage.month_key())
    return usage.has_active_access(state)


def _record_teardown(db: Session, public_key: str, tunnel_ip: str,
                     subdomain: str, error: str) -> None:
    """Remember a peer the agent still has to remove, for the sweep to retry."""
    if not public_key:
        return
    db.add(TunnelTeardown(public_key=public_key, tunnel_ip=tunnel_ip or "",
                          subdomain=subdomain or "", attempts=1,
                          last_error=str(error)[:300],
                          created_at=utc_now_iso()))


def teardown_peer(db: Session, peer: TunnelPeer) -> bool:
    """Ask the agent to drop a peer and its route, then delete the row.

    The row goes either way, so a dead agent cannot keep a lapsed or removed
    kitchen's record alive. When the agent call fails the peer is kept as a
    TunnelTeardown instead, so the sweep finishes the job later and its
    address and name are not reused meanwhile. The caller commits. Returns
    whether the agent confirmed the removal now."""
    ok = True
    try:
        tunnel_client.remove_peer(peer.public_key)
    except tunnel_client.TunnelAgentError as exc:
        ok = False
        _record_teardown(db, peer.public_key, peer.tunnel_ip, peer.subdomain,
                         str(exc))
    inst = db.get(Instance, peer.instance_id)
    if inst:
        inst.public_url = ""
    db.delete(peer)
    return ok


def teardown_instance_tunnel(db: Session, inst: Instance) -> None:
    """Take a kitchen's tunnel down before the kitchen itself is deleted.

    Deleting the instance would otherwise cascade the TunnelPeer row away on
    Postgres while the WireGuard peer and Caddy route stay live on the VPS,
    and the next kitchen handed the same address or name would inherit them.
    Every instance delete calls this first. The caller commits."""
    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    if peer:
        teardown_peer(db, peer)
        db.flush()


def disable_tunnel_for_account(db: Session, account_id: int) -> int:
    """Tear down every tunnel an account owns. Called when a plan lapses (from
    the admin panel, a Stripe cancellation, or the entitlement sweep), so
    remote access stops the moment the entitlement does. A row is removed
    even if the agent call fails (the removal is queued for the sweep), so a
    dead agent cannot pin a lapsed tunnel open. Returns how many peers were
    removed."""
    peers = db.query(TunnelPeer).filter_by(account_id=account_id).all()
    for peer in peers:
        teardown_peer(db, peer)
    if peers:
        db.commit()
    return len(peers)


def retry_teardowns(db: Session) -> int:
    """One pass over the queued teardowns: ask the agent again for each and
    forget the ones it confirms. Returns how many were cleared."""
    cleared = 0
    for row in db.query(TunnelTeardown).all():
        try:
            tunnel_client.remove_peer(row.public_key)
        except tunnel_client.TunnelAgentError as exc:
            row.attempts = (row.attempts or 0) + 1
            row.last_error = str(exc)[:300]
            continue
        db.delete(row)
        cleared += 1
    db.commit()
    return cleared


def sweep_unentitled_tunnels(db: Session) -> int:
    """Tear down the tunnels of every account that no longer has remote
    access: a trial that ran out on its own, a plan that lapsed without a
    webhook reaching us. The on-demand paths (admin disable, Stripe
    cancellation) already cover the rest. Returns how many tunnels came down."""
    removed = 0
    account_ids = {a for (a,) in db.query(TunnelPeer.account_id).distinct()}
    for account_id in sorted(account_ids):
        if not _entitled(db, account_id):
            removed += disable_tunnel_for_account(db, account_id)
    return removed


def _held_subdomains(db: Session) -> list[str]:
    """Every subdomain in use: live peers plus any still waiting on teardown."""
    return ([s for (s,) in db.query(TunnelPeer.subdomain).all()]
            + [s for (s,) in db.query(TunnelTeardown.subdomain).all() if s])


def _held_ips(db: Session) -> list[str]:
    return ([ip for (ip,) in db.query(TunnelPeer.tunnel_ip).all()]
            + [ip for (ip,) in db.query(TunnelTeardown.tunnel_ip).all() if ip])


def _unavailable(message: str) -> HTTPException:
    return HTTPException(503, detail={"error": "tunnel_agent_unavailable",
                                      "message": message})


@router.post("/enable")
def enable_tunnel(payload: EnableRequest,
                  inst: Instance = Depends(current_instance),
                  db: Session = Depends(get_db)):
    if not _entitled(db, inst.account_id):
        raise HTTPException(402, detail={
            "error": "no_subscription",
            "message": "Remote access needs an active Forager plan or trial. "
                       "Subscribe on the Forager website to reach your kitchen "
                       "from anywhere.",
        })

    limit = settings.tunnel_enable_rate_per_minute
    if (not ratelimit.allow(f"tunnel-enable-inst:{inst.id}", limit)
            or not ratelimit.allow(f"tunnel-enable-acct:{inst.account_id}",
                                   limit)):
        raise HTTPException(429, detail={
            "error": "rate_limited",
            "message": "Remote access was changed too many times in a row. "
                       "Wait a minute and try again.",
        })

    public_key = payload.public_key.strip()
    if not public_key:
        raise HTTPException(400, detail="A WireGuard public key is required")
    if not alloc.valid_wireguard_key(public_key):
        raise HTTPException(400, detail={
            "error": "invalid_public_key",
            "message": "That is not a valid WireGuard public key.",
        })

    app_port = payload.app_port if payload.app_port is not None else 9284
    if not alloc.valid_app_port(app_port):
        raise HTTPException(400, detail={
            "error": "invalid_app_port",
            "message": "The app port must be a number from 1 to 65535.",
        })

    with _ENABLE_LOCK:
        return _enable_locked(db, inst, public_key, app_port, payload)


def _enable_locked(db: Session, inst: Instance, public_key: str, app_port: int,
                   payload: EnableRequest) -> dict:
    """The allocating half of enable, run under _ENABLE_LOCK."""
    # A WireGuard key names exactly one peer on the VPS. Accepting a key some
    # other kitchen already uses would move that kitchen's peer to this one's
    # address, and this kitchen's later disable would remove it. A key still
    # queued for removal would be taken down again by the sweep.
    key_owner = (db.query(TunnelPeer)
                 .filter(TunnelPeer.public_key == public_key,
                         TunnelPeer.instance_id != inst.id).first())
    key_queued = (db.query(TunnelTeardown)
                  .filter_by(public_key=public_key).first())
    if key_owner or key_queued:
        raise HTTPException(409, detail={
            "error": "public_key_in_use",
            "message": "Remote access could not be set up with this key. "
                       "Turn remote access off and on again to make a new "
                       "one.",
        })

    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    cap = settings.tunnel_max_per_account
    if not peer and cap > 0:
        held = db.query(TunnelPeer).filter_by(account_id=inst.account_id).count()
        if held >= cap:
            raise HTTPException(409, detail={
                "error": "tunnel_limit",
                "message": f"This account already has remote access on {held} "
                           "kitchens, the most it can have. Turn it off on "
                           "one of them first.",
            })
    # Subdomains already handed out (or still being torn down), minus this
    # kitchen's own, so re-enabling with your current name never collides
    # with yourself.
    existing_subs = _held_subdomains(db)
    own_sub = peer.subdomain if peer else None
    others = [s for s in existing_subs if s != own_sub]

    requested = (payload.subdomain or "").strip()
    if requested:
        # The kitchen chose a web address: sanitize it and make sure no one
        # else already holds it, otherwise hand back a free alternative.
        desired = alloc.sanitize_subdomain(requested)
        if alloc.is_reserved_subdomain(desired):
            raise HTTPException(409, detail={
                "error": "subdomain_reserved",
                "message": "That web address is reserved. Try another one.",
                "suggestion": alloc.ensure_unique_subdomain(
                    alloc.claimable_base(desired), others),
            })
        if desired in {str(s).strip().lower() for s in others}:
            raise HTTPException(409, detail={
                "error": "subdomain_taken",
                "message": "That web address is already taken. Try another one.",
                "suggestion": alloc.ensure_unique_subdomain(desired, others),
            })
        subdomain = desired
    elif peer:
        # Already has a tunnel and did not ask for a new name: keep it stable.
        subdomain = peer.subdomain
    else:
        base = alloc.claimable_base(
            alloc.sanitize_subdomain(payload.hostname_hint or inst.name))
        subdomain = alloc.ensure_unique_subdomain(base, others)

    if peer and peer.public_key != public_key:
        # The app makes a fresh keypair each time remote access is turned on.
        # Take the old key off the VPS first, or its peer and Caddy site would
        # stay behind next to the new one.
        try:
            tunnel_client.remove_peer(peer.public_key)
        except tunnel_client.TunnelAgentError as exc:
            raise _unavailable("Remote access could not be set up right now. "
                               "Please try again in a few minutes.") from exc

    if peer:
        # Refresh the public key (the app may have rotated its keypair) and the
        # app port (a device can move between Pi and server shapes), and adopt
        # the resolved subdomain (unchanged unless a new one was chosen). The IP
        # stays stable.
        peer.public_key = public_key
        peer.app_port = app_port
        peer.subdomain = subdomain
        tunnel_ip = peer.tunnel_ip
    else:
        existing_ips = _held_ips(db)
        tunnel_ip = alloc.allocate_ip(existing_ips, settings.tunnel_cidr)

    # Program the VPS first; only persist once the peer is really in place.
    try:
        tunnel_client.add_peer(public_key, tunnel_ip, _dns_name(subdomain),
                               app_port=app_port)
    except tunnel_client.TunnelAgentError as exc:
        raise HTTPException(503, detail={
            "error": "tunnel_agent_unavailable",
            "message": "Remote access could not be set up right now. Please "
                       "try again in a few minutes.",
        }) from exc

    if not peer:
        peer = TunnelPeer(instance_id=inst.id, account_id=inst.account_id,
                          public_key=public_key, tunnel_ip=tunnel_ip,
                          app_port=app_port, subdomain=subdomain,
                          created_at=utc_now_iso())
        db.add(peer)
    inst.public_url = _public_url(subdomain)
    db.commit()

    return {
        "server_public_key": settings.tunnel_server_public_key,
        "server_endpoint": settings.tunnel_endpoint,
        "tunnel_ip": tunnel_ip,
        "tunnel_cidr": settings.tunnel_cidr,
        "dns_name": _dns_name(subdomain),
        "public_url": _public_url(subdomain),
        "keepalive": 25,
        "allowed_ips": alloc.SERVER_ALLOWED_IPS,
    }


@router.post("/disable")
def disable_tunnel(inst: Instance = Depends(current_instance),
                   db: Session = Depends(get_db)):
    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    if peer:
        try:
            tunnel_client.remove_peer(peer.public_key)
        except tunnel_client.TunnelAgentError as exc:
            raise HTTPException(503, detail={
                "error": "tunnel_agent_unavailable",
                "message": "Remote access could not be turned off right now. "
                           "Please try again in a few minutes.",
            }) from exc
        db.delete(peer)
    inst.public_url = ""
    db.commit()
    return {"disabled": True}


@router.get("/status")
def tunnel_status(inst: Instance = Depends(current_instance),
                  db: Session = Depends(get_db)):
    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    if not peer:
        return {"enabled": False, "dns_name": "", "public_url": "",
                "tunnel_ip": "", "last_handshake": ""}
    return {
        "enabled": True,
        "dns_name": _dns_name(peer.subdomain),
        "public_url": _public_url(peer.subdomain),
        "tunnel_ip": peer.tunnel_ip,
        "last_handshake": peer.last_handshake,
    }


@router.get("/subdomain-available")
def subdomain_available(name: str = Query(...),
                        inst: Instance = Depends(current_instance),
                        db: Session = Depends(get_db)):
    """Is a chosen web address free? Sanitizes the candidate the same way enable
    does, checks it against every other kitchen's subdomain (a kitchen's own
    name never counts against it, so re-checking your current address stays
    available), and returns a free alternative when it is taken.
    """
    sanitized = alloc.sanitize_subdomain(name)
    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    own_sub = peer.subdomain if peer else None
    others = [s for s in _held_subdomains(db) if s != own_sub]
    taken = {str(s).strip().lower() for s in others}
    reserved = alloc.is_reserved_subdomain(sanitized)
    available = sanitized not in taken and not reserved
    suggestion = (sanitized if available else alloc.ensure_unique_subdomain(
        alloc.claimable_base(sanitized), others))
    return {
        "available": available,
        "reserved": reserved,
        "sanitized": sanitized,
        "suggestion": suggestion,
        "apex": _apex_domain(),
    }


@router.get("/tls-check")
def tls_check(domain: str = Query(...), db: Session = Depends(get_db)):
    """Caddy on_demand_tls ask target: may Caddy issue a certificate for this
    domain? Answers 200 only for a known kitchen subdomain with a live peer
    whose account still has remote access, 404 otherwise. Unauthenticated on purpose (Caddy calls it with no
    credentials) and kept to a single indexed lookup so it stays fast.
    """
    host = (domain or "").strip().lower().rstrip(".")
    suffix = "." + _apex_domain()
    if not host.endswith(suffix):
        raise HTTPException(404, detail="unknown domain")
    label = host[: -len(suffix)]
    if not label or "." in label:
        raise HTTPException(404, detail="unknown domain")
    peer = db.query(TunnelPeer).filter_by(subdomain=label).first()
    if not peer:
        raise HTTPException(404, detail="unknown domain")
    # A lapsed account gets no new certificate, even in the window before the
    # sweep takes its tunnel down.
    if not _entitled(db, peer.account_id):
        raise HTTPException(404, detail="unknown domain")
    return {"allow": True}
