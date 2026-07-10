"""System resources collector (FoodAssistant-do2u).

Pure parser tests fed captured /proc and /sys text, omission behaviour when a
source is missing, and a TestClient shape test for the /setup/resources route.
"""
import os
from pathlib import Path

import pytest

from app.services import resources


# Captured /proc/stat shape (2 cores), two samples ~a tick apart.
PROC_STAT_A = """\
cpu  100 0 100 700 100 0 0 0 0 0
cpu0 60 0 50 350 40 0 0 0 0 0
cpu1 40 0 50 350 60 0 0 0 0 0
intr 12345 0 0
ctxt 999
btime 1700000000
"""
PROC_STAT_B = """\
cpu  150 0 150 750 150 0 0 0 0 0
cpu0 110 0 100 350 40 0 0 0 0 0
cpu1 40 0 50 400 110 0 0 0 0 0
intr 12399 0 0
"""

# Captured /proc/meminfo (abridged), 8 GB Pi-ish numbers in kB.
MEMINFO = """\
MemTotal:        8000000 kB
MemFree:         2000000 kB
MemAvailable:    5000000 kB
Buffers:          300000 kB
Cached:          1500000 kB
SwapTotal:       1000000 kB
SwapFree:         750000 kB
"""


class TestParseProcStat:
    def test_parses_aggregate_and_cores(self):
        d = resources.parse_proc_stat(PROC_STAT_A)
        assert set(d) == {"cpu", "cpu0", "cpu1"}
        # cpu: total 1000, idle 700 + iowait 100 => busy 200
        assert d["cpu"] == (200, 1000)
        assert d["cpu0"] == (110, 500)

    def test_skips_non_cpu_and_garbage(self):
        assert resources.parse_proc_stat("intr 1 2 3\ncpu one two\n") == {}
        assert resources.parse_proc_stat("") == {}
        assert resources.parse_proc_stat(None) == {}


class TestCpuPercents:
    def test_delta_between_samples(self):
        a = resources.parse_proc_stat(PROC_STAT_A)
        b = resources.parse_proc_stat(PROC_STAT_B)
        out = resources.cpu_percents(a, b)
        # cpu: busy +100 of total +200
        assert out["percent"] == pytest.approx(50.0, abs=0.1)
        assert out["per_core"] == [100.0, 0.0]

    def test_no_advance_is_omitted(self):
        a = resources.parse_proc_stat(PROC_STAT_A)
        assert resources.cpu_percents(a, a) == {}

    def test_missing_samples_are_omitted(self):
        assert resources.cpu_percents({}, {}) == {}


class TestParseMeminfo:
    def test_uses_memavailable(self):
        m = resources.parse_meminfo(MEMINFO)
        assert m["total"] == 8000000 * 1024
        assert m["available"] == 5000000 * 1024
        assert m["used"] == 3000000 * 1024
        assert m["percent"] == 37.5
        assert m["swap"]["used"] == 250000 * 1024
        assert m["swap"]["percent"] == 25.0

    def test_old_kernel_without_memavailable(self):
        text = "MemTotal: 1000 kB\nMemFree: 200 kB\nBuffers: 100 kB\nCached: 100 kB\n"
        m = resources.parse_meminfo(text)
        assert m["available"] == 400 * 1024
        assert m["percent"] == 60.0
        assert "swap" not in m  # no swap lines: no swap section

    def test_zero_swap_is_omitted(self):
        text = MEMINFO.replace("SwapTotal:       1000000 kB", "SwapTotal: 0 kB")
        assert "swap" not in resources.parse_meminfo(text)

    def test_garbage_returns_empty(self):
        assert resources.parse_meminfo("") == {}
        assert resources.parse_meminfo("nonsense") == {}


class TestUptimeAndLoad:
    def test_parse_uptime(self):
        assert resources.parse_uptime("354862.24 1385723.62\n") == pytest.approx(354862.24)
        assert resources.parse_uptime("") is None
        assert resources.parse_uptime("abc") is None

    def test_format_uptime(self):
        assert resources.format_uptime(30) == "less than a minute"
        assert resources.format_uptime(5 * 60) == "5 minutes"
        assert resources.format_uptime(3 * 3600 + 120) == "3 hours"
        assert resources.format_uptime(2 * 86400 + 5 * 3600) == "2 days, 5 hours"
        assert resources.format_uptime(86400) == "1 day"

    def test_parse_loadavg(self):
        assert resources.parse_loadavg("0.52 0.58 0.59 1/389 12345\n") == [0.52, 0.58, 0.59]
        assert resources.parse_loadavg("") is None


class TestParseThermal:
    def test_millidegrees(self):
        assert resources.parse_thermal("48534\n") == 48.5

    def test_plain_degrees_fallback(self):
        assert resources.parse_thermal("47") == 47.0

    def test_implausible_or_garbage_is_none(self):
        assert resources.parse_thermal("999999") is None
        assert resources.parse_thermal("") is None
        assert resources.parse_thermal("hot") is None


class TestDecodeThrottleWord:
    def test_all_clear(self):
        d = resources.decode_throttle_word(0)
        assert d["ok"] is True
        assert d["live"] == [] and d["since_boot"] == []

    def test_live_and_sticky(self):
        # 0x50005: undervoltage + throttled live, and their since-boot bits.
        d = resources.decode_throttle_word(0x50005)
        assert d["ok"] is False
        assert len(d["live"]) == 2
        assert "Under-voltage" in d["live"][0]
        assert d["since_boot"] == []  # live wins over the sticky flag

    def test_sticky_only(self):
        d = resources.decode_throttle_word(0x50000)
        assert d["ok"] is True
        assert d["live"] == []
        assert len(d["since_boot"]) == 2

    def test_non_int_returns_empty(self):
        assert resources.decode_throttle_word(None) == {}
        assert resources.decode_throttle_word("0x0") == {}


class TestDiskMetrics:
    def test_same_filesystem_collapses(self, tmp_path):
        sub = tmp_path / "data"
        sub.mkdir()
        disks = resources.disk_metrics([("App data", str(sub)), ("System disk", str(tmp_path))])
        assert len(disks) == 1
        d = disks[0]
        assert d["label"] == "App data"
        assert d["total"] > 0 and 0 <= d["percent"] <= 100

    def test_missing_path_is_skipped(self, tmp_path):
        disks = resources.disk_metrics([
            ("Gone", str(tmp_path / "nope")),
            ("Here", str(tmp_path)),
        ])
        assert [d["label"] for d in disks] == ["Here"]


class TestCollect:
    def _fixture_roots(self, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "stat").write_text(PROC_STAT_B)
        (proc / "meminfo").write_text(MEMINFO)
        (proc / "uptime").write_text("354862.24 1385723.62\n")
        (proc / "loadavg").write_text("0.52 0.58 0.59 1/389 12345\n")
        sysr = tmp_path / "sys"
        zone = sysr / "class" / "thermal" / "thermal_zone0"
        zone.mkdir(parents=True)
        (zone / "temp").write_text("48534\n")
        return str(proc), str(sysr)

    def test_full_environment(self, tmp_path):
        proc, sysr = self._fixture_roots(tmp_path)
        prev = resources.parse_proc_stat(PROC_STAT_A)
        cur = resources.parse_proc_stat(PROC_STAT_B)
        out = resources.collect(str(tmp_path), prev, cur, proc=proc, sys_root=sysr)
        assert out["cpu"]["percent"] > 0
        assert out["cpu"]["cores"] == 2
        assert out["cpu"]["load"] == [0.52, 0.58, 0.59]
        assert out["memory"]["percent"] == 37.5
        assert out["uptime"]["seconds"] == 354862
        assert "day" in out["uptime"]["text"]
        assert out["temperature"]["celsius"] == 48.5
        assert out["disks"][0]["label"] == "App data"

    def test_bare_environment_omits_everything_but_disks(self, tmp_path):
        # Empty proc/sys roots: every /proc- and /sys-backed metric is simply
        # absent, and only the (real) disk usage of tmp_path remains.
        proc = tmp_path / "proc"
        proc.mkdir()
        sysr = tmp_path / "sys"
        sysr.mkdir()
        out = resources.collect(str(tmp_path), {}, {}, proc=str(proc), sys_root=str(sysr))
        assert "cpu" not in out
        assert "memory" not in out
        assert "uptime" not in out
        assert "temperature" not in out
        assert "disks" in out

    def test_never_raises_on_unreadable_roots(self, tmp_path):
        out = resources.collect(str(tmp_path), {}, {},
                                proc=str(tmp_path / "nope"), sys_root=str(tmp_path / "nada"))
        assert isinstance(out, dict)


class TestResourcesRoute:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        service_dir = Path(__file__).parent.parent / "service"
        monkeypatch.chdir(service_dir)
        from app.config import settings

        settings.data_dir = str(tmp_path)
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "k"
        settings.vision_provider = "none"
        settings.auth_required = False
        settings.auth_password = ""
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_shape(self, client):
        r = client.get("/setup/resources")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        # On any Linux host running the suite these are readable.
        if "cpu" in d:
            assert isinstance(d["cpu"], dict)
        assert "disks" in d
        for disk in d["disks"]:
            assert {"label", "total", "used", "free", "percent"} <= set(disk)
        if "memory" in d:
            assert 0 <= d["memory"]["percent"] <= 100
        if "uptime" in d:
            assert isinstance(d["uptime"]["text"], str)
