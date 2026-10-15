"""Numbers in text are the ASCII digits only (FoodAssistant-y0m7b).

"²".isdigit() is True and int("²") raises, so every check written as
isdigit() followed by int() answered 500 for a superscript or circled digit.
"""
from __future__ import annotations

import pytest

from app.plain_digits import is_plain_digits, plain_int

JUNK = ["²", "①", "W²", "12²", "٣", "１２", "", " 12", "12 ", "-1", "+1", "1.0", None, 7, ["1"]]


@pytest.mark.parametrize("value", JUNK)
def test_only_ascii_digits_count(value):
    assert is_plain_digits(value) is False
    assert plain_int(value) is None


def test_plain_digits_read_as_numbers():
    assert is_plain_digits("0123")
    assert plain_int("0123") == 123
    assert plain_int("9" * 18) == int("9" * 18)
    # Too long to be an id: refused rather than handed to int().
    assert plain_int("9" * 19) is None
    assert plain_int("99", max_digits=1) is None


@pytest.mark.parametrize("junk", ["²", "①", "²²²²²²²²²²²²", "12345678901²"])
def test_the_former_crash_sites_answer_instead_of_raising(junk):
    from app.routers.pending import gtin_check_digit_ok
    from app.services.barcode import is_store_local_barcode
    from app.services.cameras import resolve_camera_index
    from app.services.expiry_learning import build_point
    from app.services.grocy import parse_grocycode
    from app.totp import totp_verify

    assert gtin_check_digit_ok(junk) is True      # not a GTIN: let it through
    assert is_store_local_barcode(junk) is False
    assert is_store_local_barcode("2" + "²" * 11) is False
    assert is_store_local_barcode("02²" + "0" * 10) is False
    assert parse_grocycode(f"grcy:p:{junk}") is None
    assert resolve_camera_index([{"name": "a"}, {"name": "b"}], junk) == 0
    assert resolve_camera_index([{"name": "a"}, {"name": "b"}], "-" + junk) == 0
    assert totp_verify("JBSWY3DPEHPK3PXP", "²²²²²²") is False
    from datetime import date
    assert build_point("milk", junk, "fridge", date(2026, 1, 9), None, None,
                       date(2026, 1, 1))["barcode"] in (None, "")


def test_camera_index_still_reads_plain_numbers():
    from app.services.cameras import resolve_camera_index
    cams = [{"name": "a"}, {"name": "b"}]
    assert resolve_camera_index(cams, "1") == 1
    assert resolve_camera_index(cams, "-1") == 0


def test_tunnel_prefix_refuses_unicode_digits():
    from app.services import tunnel_local
    with pytest.raises(ValueError, match="invalid WireGuard address"):
        tunnel_local._checked_address("10.0.0.2/²")
    assert tunnel_local._checked_address("10.0.0.2/32") == "10.0.0.2/32"


def test_meal_plan_resolves_a_superscript_id_as_a_slug(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.services import meal_plan

    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        assert meal_plan._resolve_recipe(db, "²") is None
        assert meal_plan._resolve_recipe(db, "9" * 40) is None
    finally:
        db.close()
        engine.dispose()


def test_printing_server_port_ignores_unicode_digits():
    from app.services.printing import _split_server
    assert _split_server("cups.local:²") == ("cups.local", "")
    assert _split_server("cups.local:631") == ("cups.local", "631")
