"""Guard the app version format.

APP_VERSION is the single source of truth for the app version (UI, update
checker, FastAPI, satellite heartbeat). It must stay valid major.minor.patch
semver so the bump tooling and the update checker can parse it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import APP_VERSION  # noqa: E402

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_app_version_is_semver():
    assert _SEMVER.match(APP_VERSION), f"APP_VERSION {APP_VERSION!r} is not X.Y.Z"


def test_app_version_is_pre_1_0():
    # The project reserves 1.0.0 for the first public release; everything to
    # date is pre-launch 0.x. Drop this assertion when 1.0.0 actually ships.
    major = int(APP_VERSION.split(".")[0])
    assert major == 0, f"APP_VERSION {APP_VERSION!r}: 1.0.0 is reserved for launch"
