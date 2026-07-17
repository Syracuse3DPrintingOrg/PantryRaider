"""BLE broadcast payload packer/unpacker (FoodAssistant-yl6u).

Pure-logic tests for gadgets/foodassistant_gadgets/advertiser.py: exact byte
vectors, round-trips, clamping, sentinels, the view/alert nibble packing, and
the changed-bytes sequence step. No D-Bus, no radio, no network. The shared
vectors are also written to tests/data/cub_ble_vectors.json so the ESPHome
receiver's parser tests can consume the same set.
"""
import json
from pathlib import Path

import pytest

from foodassistant_gadgets import advertiser as adv

NOW = 1750000000
DEVICE = "aabbccdd00112233"

BASE = {
    "view": "clock", "timers": [], "probes": [], "alerts": [],
    "expiring": {"expired": 0, "today": 0, "soon": 0},
    "counts": {"pending": 0},
}


def summary(**over):
    out = {**BASE, **over}
    return out


# -- shared vectors -------------------------------------------------------------


def test_vectors_pack_exactly():
    for vec in adv.VECTORS:
        packed = adv.pack_status(vec["summary"], vec["device_id"],
                                 vec["seq"], now=vec["now"])
        assert packed.hex() == vec["hex"], vec["name"]


def test_vectors_unpack_round_trip():
    for vec in adv.VECTORS:
        decoded = adv.unpack_status(bytes.fromhex(vec["hex"]))
        assert decoded["version"] == adv.FORMAT_VERSION, vec["name"]
        assert decoded["seq"] == vec["seq"], vec["name"]
        assert decoded["install_tag"] == \
            adv.install_tag(vec["device_id"]).hex(), vec["name"]


def test_vectors_file_matches(tmp_path):
    """The committed vectors file is exactly the exported VECTORS data,
    regenerated deterministically. If this fails after a payload change,
    rerun with FOODASSISTANT_WRITE_VECTORS=1 semantics: the test writes the
    file itself when missing or stale, then fails once so the refreshed file
    gets reviewed and committed."""
    path = Path(__file__).parent / "data" / "cub_ble_vectors.json"
    expected = json.dumps(adv.VECTORS, indent=2, sort_keys=True) + "\n"
    if not path.exists() or path.read_text() != expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected)
        pytest.fail(f"{path} regenerated from advertiser.VECTORS; "
                    "review and commit it")
    assert json.loads(path.read_text()) == adv.VECTORS


# -- layout basics ---------------------------------------------------------------


def test_total_length_and_headers():
    packet = adv.pack_status(summary(), DEVICE, 0, now=NOW)
    assert len(packet) == 23
    assert packet[:3] == bytes((0x02, 0x01, 0x06))          # Flags AD
    assert packet[3] == 0x13                                  # MSD length 19
    assert packet[4] == 0xFF                                  # MSD type
    assert packet[5:7] == b"\xff\xff"                        # company id
    assert packet[7] == adv.FORMAT_VERSION


def test_msd_payload_is_what_bluez_gets():
    packet = adv.pack_status(summary(), DEVICE, 9, now=NOW)
    payload = adv.msd_payload(packet)
    assert len(payload) == 16
    assert payload[0] == adv.FORMAT_VERSION
    assert payload[1] == 9
    # MSD length byte covers type + company id + this payload.
    assert packet[3] == 1 + 2 + len(payload)


def test_install_tag_is_sha256_prefix():
    import hashlib
    packet = adv.pack_status(summary(), DEVICE, 0, now=NOW)
    assert packet[-4:] == hashlib.sha256(DEVICE.encode()).digest()[:4]
    assert adv.install_tag("a") != adv.install_tag("b")


def test_round_trip_full_state():
    s = summary(
        view="timers",
        timers=[{"id": "t1", "deadline_epoch": NOW + 90, "expired": False}],
        probes=[{"id": "P", "probe": 1, "temp_c": 55.0, "target_c": 71.0,
                 "direction": "above", "stale": False}],
        alerts=[{"kind": "contact"}],
        expiring={"expired": 1, "today": 2, "soon": 3},
        counts={"pending": 4},
    )
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 5, now=NOW))
    assert decoded == {
        "version": 1, "seq": 5, "view": 2,
        "flags": {"timer_ringing": False, "probe_at_target": False,
                  "attention": True},
        "expired": 1, "soon": 5, "pending": 4, "timer_count": 1,
        "soonest_timer_s": 90, "probe_temp_c": 55.0, "probe_delta_c": 16,
        "install_tag": adv.install_tag(DEVICE).hex(),
    }


# -- view hint and alert flags ----------------------------------------------------


@pytest.mark.parametrize("view,expect", [
    ("clock", 0), ("rotation", 0), ("alert", 0), ("", 0), (None, 0),
    ("something-new", 0), ("expiring", 1), ("timers", 2), ("probe", 3),
])
def test_view_hint_nibble(view, expect):
    decoded = adv.unpack_status(
        adv.pack_status(summary(view=view), DEVICE, 0, now=NOW))
    assert decoded["view"] == expect


def test_timer_ringing_flag():
    s = summary(timers=[{"id": "t", "deadline_epoch": NOW - 5,
                         "expired": True}])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["flags"]["timer_ringing"] is True
    assert decoded["soonest_timer_s"] == 0


@pytest.mark.parametrize("temp,target,direction,expect", [
    (93.0, 93.0, "above", True),
    (94.0, 93.0, "above", True),
    (92.9, 93.0, "above", False),
    (3.9, 4.0, "below", True),
    (4.1, 4.0, "below", False),
])
def test_probe_at_target_flag(temp, target, direction, expect):
    s = summary(probes=[{"id": "P", "probe": 1, "temp_c": temp,
                         "target_c": target, "direction": direction,
                         "stale": False}])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["flags"]["probe_at_target"] is expect


def test_attention_flag_tracks_alerts_block():
    on = summary(alerts=[{"kind": "hygrometer"}])
    off = summary(alerts=[])
    assert adv.unpack_status(adv.pack_status(on, DEVICE, 0, now=NOW)
                             )["flags"]["attention"] is True
    assert adv.unpack_status(adv.pack_status(off, DEVICE, 0, now=NOW)
                             )["flags"]["attention"] is False


# -- counts, clamping, sentinels ---------------------------------------------------


def test_counts_clamp_to_255():
    s = summary(
        timers=[{"id": str(i), "deadline_epoch": NOW + 100000}
                for i in range(256)],
        expiring={"expired": 300, "today": 200, "soon": 200},
        counts={"pending": 999},
    )
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["timer_count"] == 255
    assert decoded["expired"] == 255
    assert decoded["soon"] == 255
    assert decoded["pending"] == 255


def test_today_counts_as_soon():
    s = summary(expiring={"expired": 0, "today": 2, "soon": 3})
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["soon"] == 5


def test_no_timers_sentinel():
    decoded = adv.unpack_status(adv.pack_status(summary(), DEVICE, 0, now=NOW))
    assert decoded["soonest_timer_s"] is None
    assert decoded["timer_count"] == 0


def test_soonest_timer_picks_minimum_and_clamps():
    s = summary(timers=[
        {"id": "a", "deadline_epoch": NOW + 120},
        {"id": "b", "deadline_epoch": NOW + 60},
        {"id": "c", "deadline_epoch": NOW + 999999},
    ])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["soonest_timer_s"] == 60
    only_long = summary(timers=[{"id": "c", "deadline_epoch": NOW + 999999}])
    decoded = adv.unpack_status(adv.pack_status(only_long, DEVICE, 0, now=NOW))
    assert decoded["soonest_timer_s"] == 0xFFFE  # clamped, still "a timer"


def test_no_probe_sentinels():
    decoded = adv.unpack_status(adv.pack_status(summary(), DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] is None
    assert decoded["probe_delta_c"] is None


def test_probe_without_target_has_temp_but_no_delta():
    s = summary(probes=[{"id": "P", "probe": 1, "temp_c": 21.7,
                         "target_c": None, "stale": False}])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] == 21.7
    assert decoded["probe_delta_c"] is None


def test_probe_with_target_preferred_over_first():
    s = summary(probes=[
        {"id": "A", "probe": 1, "temp_c": 20.0, "target_c": None,
         "stale": False},
        {"id": "B", "probe": 1, "temp_c": 50.0, "target_c": 70.0,
         "direction": "above", "stale": False},
    ])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] == 50.0
    assert decoded["probe_delta_c"] == 20


def test_stale_probes_ignored():
    s = summary(probes=[{"id": "P", "probe": 1, "temp_c": 55.0,
                         "target_c": 70.0, "stale": True}])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] is None


def test_extreme_temps_and_deltas_clamp_off_the_sentinels():
    s = summary(probes=[{"id": "P", "probe": 1, "temp_c": 5000.0,
                         "target_c": 90000.0, "direction": "above",
                         "stale": False}])
    decoded = adv.unpack_status(adv.pack_status(s, DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] == 3276.6   # 0x7FFE tenths, not the sentinel
    assert decoded["probe_delta_c"] == 126     # 0x7E, not the 0x7F sentinel
    cold = summary(probes=[{"id": "P", "probe": 1, "temp_c": -5000.0,
                            "target_c": -9000.0, "direction": "below",
                            "stale": False}])
    decoded = adv.unpack_status(adv.pack_status(cold, DEVICE, 0, now=NOW))
    assert decoded["probe_temp_c"] == -3276.8
    assert decoded["probe_delta_c"] == -128


def test_no_strings_can_ride_along():
    """The never-leak rule, enforced structurally: junk strings anywhere in
    the summary never change the packet size and never crash the packer."""
    s = summary(
        view="timers",
        timers=[{"id": "secret-name", "label": "Dan's roast",
                 "deadline_epoch": "soon", "expired": "yes"}],
        probes=[{"id": "MAC", "name": "secret", "temp_c": "hot",
                 "target_c": "warm", "stale": False}],
        alerts=[{"message": "the fridge is warm"}],
        expiring={"expired": "many", "today": [], "soon": {}},
        counts={"pending": "lots"},
    )
    packet = adv.pack_status(s, DEVICE, 0, now=NOW)
    assert len(packet) == 23
    decoded = adv.unpack_status(packet)
    assert decoded["expired"] == 0 and decoded["pending"] == 0


def test_malformed_summary_shapes_never_raise():
    for bad in ({}, {"timers": "x", "probes": 3, "expiring": [],
                 "counts": None, "alerts": "?"}, {"view": 42}):
        assert len(adv.pack_status(bad, DEVICE, 0, now=NOW)) == 23


# -- unpack validation -------------------------------------------------------------


def test_unpack_rejects_foreign_packets():
    good = adv.pack_status(summary(), DEVICE, 0, now=NOW)
    with pytest.raises(ValueError):
        adv.unpack_status(good[:-1])
    with pytest.raises(ValueError):
        adv.unpack_status(b"\x00" * 23)
    wrong_version = bytearray(good)
    wrong_version[7] = 2
    with pytest.raises(ValueError):
        adv.unpack_status(bytes(wrong_version))


# -- sequence / changed-bytes logic --------------------------------------------------


def test_seq_stays_on_unchanged_content():
    a = adv.pack_status(summary(), DEVICE, 4, now=NOW)
    b = adv.pack_status(summary(), DEVICE, 4, now=NOW)
    assert adv.status_changed(a, b) is False
    assert adv.next_seq(a, b, 4) == 4


def test_seq_bumps_on_changed_content():
    a = adv.pack_status(summary(), DEVICE, 4, now=NOW)
    b = adv.pack_status(summary(counts={"pending": 1}), DEVICE, 4, now=NOW)
    assert adv.status_changed(a, b) is True
    assert adv.next_seq(a, b, 4) == 5


def test_seq_comparison_ignores_the_seq_byte_itself():
    a = adv.pack_status(summary(), DEVICE, 4, now=NOW)
    b = adv.pack_status(summary(), DEVICE, 200, now=NOW)
    assert adv.status_changed(a, b) is False


def test_seq_wraps_at_255():
    a = adv.pack_status(summary(), DEVICE, 255, now=NOW)
    b = adv.pack_status(summary(counts={"pending": 9}), DEVICE, 255, now=NOW)
    assert adv.next_seq(a, b, 255) == 0


def test_first_packet_keeps_starting_seq():
    b = adv.pack_status(summary(), DEVICE, 0, now=NOW)
    assert adv.next_seq(None, b, 0) == 0
    assert adv.status_changed(None, b) is True
