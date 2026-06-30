"""Fleet-wide automatic updates for Pi appliances (FoodAssistant-k2kk).

The global `auto_update` flag (on by default, pulled by satellites) decides
whether a device keeps itself current. A non-Pi server uses the Watchtower
container instead; this module only drives the Pi appliances, which apply via
the host-bridge OTA.

The decision is a pure function so it is unit-testable without a bridge or a
network: a Pi Hosted box always attempts (the OTA is idempotent and no-ops when
already current), while a Pi Remote only updates when it knows its server's
version and differs from it, so the fleet converges on the server's version
rather than a remote racing ahead to whatever is newest upstream.
"""
from __future__ import annotations


def should_run(is_satellite: bool, local_version: str, server_version: str) -> bool:
    """Whether a Pi appliance should apply an update now.

    is_satellite     - True for a Pi Remote, False for a Pi Hosted box.
    local_version    - this device's running APP_VERSION.
    server_version   - the main server's version (only meaningful for a remote);
                       '' when not yet learned from a sync.
    """
    if is_satellite:
        # Only chase the server once we have heard from it, and only when we are
        # actually on a different version (keeps the fleet aligned, avoids churn).
        return bool(server_version) and server_version != local_version
    # Pi Hosted: attempt on schedule; the OTA itself is a no-op when current.
    return True
