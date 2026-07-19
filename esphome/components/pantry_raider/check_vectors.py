#!/usr/bin/env python3
"""Vector-agreement check for the Cub BLE receiver parser.

Compiles cub_ble_parse.h host-side with g++, runs the shared test vectors
(tests/data/cub_ble_vectors.json) through the C++ parser, and compares every
field against the authoritative Python reference parser
(gadgets/foodassistant_gadgets/advertiser.py, unpack_status). Also checks the
reject paths: wrong length, wrong header, wrong format version.

Run from anywhere inside the repo:

    python3 esphome/components/pantry_raider/check_vectors.py

Needs only g++ and the repo checkout; no ESPHome, no network, no radio.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
VECTORS = REPO / "tests" / "data" / "cub_ble_vectors.json"

MAIN_CPP = r"""
#include "cub_ble_parse.h"
#include <cstdio>
#include <string>
#include <vector>
#include <iostream>

using esphome::pantry_raider::CubBlePacket;
using esphome::pantry_raider::parse_cub_advertisement;

static std::vector<uint8_t> from_hex(const std::string &hex) {
  std::vector<uint8_t> out;
  for (size_t i = 0; i + 1 < hex.size(); i += 2)
    out.push_back((uint8_t) strtol(hex.substr(i, 2).c_str(), nullptr, 16));
  return out;
}

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty())
      continue;
    std::vector<uint8_t> pkt = from_hex(line);
    CubBlePacket p;
    if (!parse_cub_advertisement(pkt.data(), pkt.size(), p)) {
      printf("REJECT\n");
      continue;
    }
    printf("seq=%u view=%u ringing=%d at_target=%d attention=%d "
           "expired=%u soon=%u pending=%u timers=%u "
           "soonest=%s temp=%s delta=%s tag=%02x%02x%02x%02x\n",
           p.seq, p.view, (int) p.timer_ringing, (int) p.probe_at_target,
           (int) p.attention, p.expired, p.soon, p.pending, p.timer_count,
           p.has_soonest ? std::to_string(p.soonest_s).c_str() : "none",
           p.has_temp ? std::to_string(p.temp_tenths).c_str() : "none",
           p.has_delta ? std::to_string((int) p.delta_c).c_str() : "none",
           p.install_tag[0], p.install_tag[1], p.install_tag[2], p.install_tag[3]);
  }
  return 0;
}
"""


def expected_line(ref: dict) -> str:
    """The C++ output line unpack_status implies for one packet."""
    soonest = ref["soonest_timer_s"]
    temp = ref["probe_temp_c"]
    delta = ref["probe_delta_c"]
    return (
        "seq={seq} view={view} ringing={r} at_target={t} attention={a} "
        "expired={expired} soon={soon} pending={pending} timers={timers} "
        "soonest={soonest} temp={temp} delta={delta} tag={tag}".format(
            seq=ref["seq"],
            view=ref["view"],
            r=int(ref["flags"]["timer_ringing"]),
            t=int(ref["flags"]["probe_at_target"]),
            a=int(ref["flags"]["attention"]),
            expired=ref["expired"],
            soon=ref["soon"],
            pending=ref["pending"],
            timers=ref["timer_count"],
            soonest="none" if soonest is None else soonest,
            temp="none" if temp is None else int(round(temp * 10)),
            delta="none" if delta is None else delta,
            tag=ref["install_tag"],
        )
    )


def main() -> int:
    sys.path.insert(0, str(REPO / "gadgets"))
    from foodassistant_gadgets.advertiser import unpack_status

    vectors = json.loads(VECTORS.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        main_cpp = Path(tmp) / "main.cpp"
        main_cpp.write_text(MAIN_CPP)
        binary = Path(tmp) / "check"
        subprocess.run(
            ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
             f"-I{HERE}", str(main_cpp), "-o", str(binary)],
            check=True,
        )
        good = [v["hex"] for v in vectors]
        # Reject cases: truncated, bad header, bad version, junk 0xFFFF MSD.
        bad = [
            good[0][:-2],                                   # 22 bytes
            "ff" + good[0][2:],                             # broken Flags AD
            good[0][:14] + "02" + good[0][16:],             # version 2
            "02010613ffffff" + "00" * 16,                   # version 0
        ]
        stdin = "\n".join(good + bad) + "\n"
        out = subprocess.run([str(binary)], input=stdin, text=True,
                             capture_output=True, check=True).stdout.splitlines()

    failures = 0
    for vec, got in zip(vectors, out[: len(good)]):
        ref = unpack_status(bytes.fromhex(vec["hex"]))
        want = expected_line(ref)
        status = "ok" if got == want else "MISMATCH"
        if got != want:
            failures += 1
            print(f"  want: {want}\n  got:  {got}")
        print(f"[{status}] {vec['name']}")
    for i, got in enumerate(out[len(good):]):
        status = "ok" if got == "REJECT" else "MISMATCH"
        if got != "REJECT":
            failures += 1
        print(f"[{status}] reject case {i}: {got}")

    if failures:
        print(f"{failures} failure(s)")
        return 1
    print(f"All {len(good)} vectors and {len(bad)} reject cases agree "
          "with unpack_status.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
