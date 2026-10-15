"""Login lockouts key on the real visitor behind a trusted proxy (security review, Oct 2026).

Behind the Forager tunnel hub or a same-host proxy every visitor shared the
proxy's address, so ten bad guesses from a stranger locked the owner out too.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import rate_limit  # noqa: E402


def _req(peer, xff=None):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_last_forwarded_entry_is_used_never_the_first():
    assert rate_limit.forwarded_client("1.2.3.4, 198.51.100.7") == "198.51.100.7"
    assert rate_limit.forwarded_client(" 203.0.113.9 ") == "203.0.113.9"
    assert rate_limit.forwarded_client("") == ""


def test_tunnel_hub_and_same_host_proxy_key_on_the_visitor():
    a = rate_limit.client_key(_req("10.99.0.1", "198.51.100.1"))
    b = rate_limit.client_key(_req("10.99.0.1", "203.0.113.50"))
    assert a != b and a.endswith("198.51.100.1")
    assert rate_limit.client_key(_req("127.0.0.1", "spoofed, 203.0.113.5")).endswith("203.0.113.5")


def test_a_spoofed_first_entry_does_not_change_the_key():
    a = rate_limit.client_key(_req("10.99.0.1", "1.1.1.1, 198.51.100.1"))
    b = rate_limit.client_key(_req("10.99.0.1", "2.2.2.2, 198.51.100.1"))
    assert a == b


def test_an_untrusted_peer_cannot_pick_its_own_key():
    # A LAN or internet client sending its own header is keyed by its address.
    assert rate_limit.client_key(_req("192.168.1.50", "203.0.113.5")) == "192.168.1.50"
    assert rate_limit.client_key(_req("198.51.100.9", "203.0.113.5")) == "198.51.100.9"
    assert rate_limit.client_key(_req("10.99.0.1")) == "10.99.0.1"


def test_a_stranger_does_not_lock_out_the_owner():
    guard = rate_limit.RateLimiter(max_attempts=3, window=300.0)
    stranger = rate_limit.client_key(_req("10.99.0.1", "198.51.100.66"))
    owner = rate_limit.client_key(_req("10.99.0.1", "203.0.113.50"))
    for _ in range(3):
        guard.record(stranger, now=100.0)
    assert guard.blocked(stranger, now=101.0)
    assert not guard.blocked(owner, now=101.0)
