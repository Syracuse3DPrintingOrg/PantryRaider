"""The /setup routes: the settings wizard, kiosk and display controls, network
and access point, Stream Deck, cameras, Forager, connection tests, updates,
and touch calibration.

Every module registers on the one shared ``router`` from ``_common``. They
are imported below in the order their routes were first written, which is
the order FastAPI matches paths in, so keep new modules at the end unless a
route has to win a match against an earlier one. A route module may import
helpers from ``_common`` or from a module listed above it, never from one
below it (that import would register the later module's routes first).

This package keeps the flat namespace the single setup.py module had, so
``from app.routers.setup import X`` keeps working for any name, and a test
that patches ``app.routers.setup.X`` also patches the copy each submodule
looks up at call time (see ``_SetupPackage``).
"""

import sys
import types

from ._common import router as router
from . import (
    _common,
    deployment,
    page,
    appearance,
    cloud,
    remote_access,
    presets,
    save,
    connections,
    totp,
    display,
    system,
    updates,
    streamdeck,
    cameras,
    streamdeck_profiles,
    access_point,
    calibrate,
)

_SUBMODULES = (
    _common, deployment, page, appearance, cloud, remote_access, presets, save,
    connections, totp, display, system, updates, streamdeck, cameras,
    streamdeck_profiles, access_point, calibrate,
)

for _mod in _SUBMODULES:
    for _name, _value in vars(_mod).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)
del _mod, _name, _value


class _SetupPackage(types.ModuleType):
    """Forward attribute writes on the package to every submodule that holds
    the same name.

    Each submodule binds its own copy of the names it imports, so patching
    ``setup.is_raspberry_pi`` or ``setup.bridge_client`` on the package alone
    would leave the routes calling the originals. Forwarding the write (and
    the restore that undoes it) keeps one patch effective everywhere, just as
    it was when everything lived in one module.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for mod in _SUBMODULES:
            if name in vars(mod):
                setattr(mod, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for mod in _SUBMODULES:
            if name in vars(mod):
                delattr(mod, name)


sys.modules[__name__].__class__ = _SetupPackage
