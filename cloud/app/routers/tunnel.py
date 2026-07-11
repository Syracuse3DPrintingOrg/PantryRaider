"""The remote-access tunnel API.

A kitchen enables remote access here: it sends its WireGuard public key and a
hostname hint, the cloud allocates a stable tunnel IP and a subdomain, tells
the VPS agent to add the WireGuard peer and the Caddy route, and hands back
everything the app needs to bring up its side of the tunnel. Remote access is
a paid or trial feature, so enable is gated on an active entitlement.

The private key never leaves the device; the cloud only ever sees the public
half. Nothing is persisted until the agent confirms the peer, so a failed
agent call leaves no orphan row.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import tunnel as alloc
from .. import tunnel_client, usage
from ..config import settings
from ..deps import current_instance, get_db, utc_now_iso
from ..models import Instance, TunnelPeer

router = APIRouter(prefix="/v1/tunnel", tags=["tunnel"])


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
    app_port: int = 9284


def _entitled(db: Session, account_id: int) -> bool:
    """Whether the account may use remote access right now: any active
    entitlement, trial or paid (the same "entitled" flag the AI proxy reads)."""
    state = usage.quota_state(db, account_id, usage.month_key())
    return bool(state["entitled"])


def disable_tunnel_for_account(db: Session, account_id: int) -> int:
    """Tear down every tunnel an account owns. Called when a plan lapses (from
    the admin panel or a Stripe cancellation), so remote access stops the
    moment the entitlement does. Best-effort on the agent side: a row is
    removed even if the agent call fails, so a dead agent cannot pin a lapsed
    tunnel open. Returns how many peers were removed.

    A periodic sweep that catches trials expiring on their own is a follow-up
    (see docs/design/forager-tunnel.md); for now this covers the on-demand
    paths and the app re-checks entitlement on its own.
    """
    peers = db.query(TunnelPeer).filter_by(account_id=account_id).all()
    removed = 0
    for peer in peers:
        try:
            tunnel_client.remove_peer(peer.public_key)
        except tunnel_client.TunnelAgentError:
            pass
        inst = db.get(Instance, peer.instance_id)
        if inst:
            inst.public_url = ""
        db.delete(peer)
        removed += 1
    if removed:
        db.commit()
    return removed


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

    public_key = payload.public_key.strip()
    if not public_key:
        raise HTTPException(400, detail="A WireGuard public key is required")

    app_port = int(payload.app_port or 9284)

    peer = db.query(TunnelPeer).filter_by(instance_id=inst.id).first()
    # Subdomains already handed out, minus this kitchen's own, so re-enabling
    # with your current name never collides with yourself.
    existing_subs = [s for (s,) in db.query(TunnelPeer.subdomain).all()]
    own_sub = peer.subdomain if peer else None
    others = [s for s in existing_subs if s != own_sub]

    requested = (payload.subdomain or "").strip()
    if requested:
        # The kitchen chose a web address: sanitize it and make sure no one
        # else already holds it, otherwise hand back a free alternative.
        desired = alloc.sanitize_subdomain(requested)
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
        base = alloc.sanitize_subdomain(payload.hostname_hint or inst.name)
        subdomain = alloc.ensure_unique_subdomain(base, others)

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
        existing_ips = [ip for (ip,) in db.query(TunnelPeer.tunnel_ip).all()]
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
    others = [s for (s,) in db.query(TunnelPeer.subdomain).all() if s != own_sub]
    taken = {str(s).strip().lower() for s in others}
    available = sanitized not in taken
    suggestion = sanitized if available else alloc.ensure_unique_subdomain(sanitized, others)
    return {
        "available": available,
        "sanitized": sanitized,
        "suggestion": suggestion,
        "apex": _apex_domain(),
    }


@router.get("/tls-check")
def tls_check(domain: str = Query(...), db: Session = Depends(get_db)):
    """Caddy on_demand_tls ask target: may Caddy issue a certificate for this
    domain? Answers 200 only for a known kitchen subdomain with a live peer,
    404 otherwise. Unauthenticated on purpose (Caddy calls it with no
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
    return {"allow": True}
