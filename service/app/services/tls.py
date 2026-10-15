"""One TLS context shared by the app's outbound HTTP clients.

httpx builds a fresh SSL context for every client it creates, and loading the
CA bundle into it takes about 10 ms on a desktop and several times that on a
Raspberry Pi. Most clients here live for one request (a barcode lookup, a
camera snapshot, a call to the host bridge), and they are created on the
event loop, so that cost landed on every other request in flight. Passing this
context as ``verify`` skips the rebuild; verification is exactly the same.
"""
from __future__ import annotations

import ssl
import threading

import httpx

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


def default_context() -> ssl.SSLContext:
    """httpx's default verifying context, built once."""
    global _context
    if _context is None:
        with _lock:
            if _context is None:
                _context = httpx.create_ssl_context()
    return _context


def verify(value=True):
    """``value`` with the plain default (True) swapped for the shared context."""
    return default_context() if value is True else value
