"""is_store_local_barcode: store-assigned/random-weight barcodes that no
catalog, and no LLM guess, can ever resolve (FoodAssistant-fv8v)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.services.barcode import is_store_local_barcode  # noqa: E402


def test_upc_a_leading_2_is_store_local():
    # UPC-A leading digit "2": restricted circulation, random weight, store-assigned.
    assert is_store_local_barcode("212345678905") is True


def test_upc_a_branded_leading_0_is_not_store_local():
    assert is_store_local_barcode("012345678905") is False


def test_ean13_020_prefix_is_store_local():
    assert is_store_local_barcode("0201234567895") is True


def test_ean13_200_prefix_is_store_local():
    assert is_store_local_barcode("2001234567891") is True


def test_ean13_299_prefix_is_store_local():
    assert is_store_local_barcode("2991234567892") is True


def test_ean13_branded_prefix_is_not_store_local():
    # A normal branded EAN-13 (e.g. a "500..." UK/Ireland prefix) is a real product code.
    assert is_store_local_barcode("5012345678900") is False


def test_ean13_030_prefix_is_not_store_local():
    # 03x is not in the 020-029 / 200-299 restricted ranges.
    assert is_store_local_barcode("0301234567891") is False


def test_non_digit_barcode_is_not_store_local():
    assert is_store_local_barcode("ABC123") is False


def test_empty_or_none_barcode_is_not_store_local():
    assert is_store_local_barcode("") is False
    assert is_store_local_barcode(None) is False


def test_odd_length_barcode_is_not_store_local():
    # Only 12 (UPC-A) and 13 (EAN-13) digit codes are classified; anything
    # else (e.g. an 8-digit UPC-E or a GS1 variable-weight code) is left alone
    # here rather than guessed at.
    assert is_store_local_barcode("2012345") is False


def test_whitespace_is_stripped():
    assert is_store_local_barcode("  212345678905  ") is True
