"""Host agent config loaders (Stream Deck controller and gadget reader).

These run straight on the host, not in the app container, so they meet
whatever Python the host has. On Python 3.10 (Ubuntu 22.04) there is no
tomllib, and both loaders fall back to the tomli backport. The Stream Deck
loader also has to survive a corrupt config.toml, because the controller
calls it at startup and a crash there restarts the unit forever.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from foodassistant_gadgets import config as gd_config
from foodassistant_streamdeck import actions as sd_actions
from foodassistant_streamdeck import config as sd_config


def _fake_tomli() -> types.ModuleType:
    """A stand-in tomli backed by the real parser, so the test can tell it ran."""
    import tomllib as real

    mod = types.ModuleType("tomli")
    mod.calls = []

    def loads(text):
        mod.calls.append(text)
        return real.loads(text)

    mod.loads = loads
    mod.TOMLDecodeError = real.TOMLDecodeError
    return mod


@pytest.mark.parametrize("module", [gd_config, sd_config], ids=["gadgets", "streamdeck"])
def test_config_imports_on_python_without_tomllib(module, monkeypatch):
    fake = _fake_tomli()
    # A None entry makes "import tomllib" raise ModuleNotFoundError, which is
    # what Python 3.10 does.
    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", fake)
    try:
        reloaded = importlib.reload(module)
        assert reloaded.tomllib is fake
        assert reloaded.tomllib.loads('base_url = "http://pantry:9284"\n') == {
            "base_url": "http://pantry:9284"
        }
        assert fake.calls
    finally:
        monkeypatch.undo()
        importlib.reload(module)
    assert module.tomllib is sys.modules["tomllib"]


@pytest.mark.parametrize("module", [gd_config, sd_config], ids=["gadgets", "streamdeck"])
def test_load_reads_file_through_tomli_fallback(module, monkeypatch, tmp_path):
    fake = _fake_tomli()
    f = tmp_path / "config.toml"
    f.write_text('base_url = "http://pantry:9284/"\napi_key = "k"\n')
    for name in ("FOODASSISTANT_BASE_URL", "FOODASSISTANT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", fake)
    try:
        cfg = importlib.reload(module).load(f)
        assert cfg.base_url == "http://pantry:9284"
        assert cfg.api_key == "k"
        assert fake.calls
    finally:
        monkeypatch.undo()
        importlib.reload(module)


# -- corrupt Stream Deck config ----------------------------------------------


def _clear_env(monkeypatch):
    for name in ("FOODASSISTANT_BASE_URL", "FOODASSISTANT_API_KEY",
                 "FOODASSISTANT_STREAMDECK_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def test_streamdeck_corrupt_config_falls_back_to_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    f = tmp_path / "config.toml"
    f.write_text('keys = ["pending", "commit"\nrotation = 90\n')  # unclosed array
    cfg = sd_config.load(f)
    assert cfg.keys == list(sd_actions.DEFAULT_ORDER)
    assert cfg.rotation == 0
    assert cfg.base_url == sd_config.Config().base_url


def test_streamdeck_corrupt_config_still_applies_env_overlay(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("FOODASSISTANT_BASE_URL", "http://server:9284/")
    monkeypatch.setenv("FOODASSISTANT_API_KEY", "from-env")
    f = tmp_path / "config.toml"
    f.write_text("this is = = not toml\n")
    cfg = sd_config.load(f)
    assert cfg.base_url == "http://server:9284"
    assert cfg.api_key == "from-env"
    assert cfg.keys == list(sd_actions.DEFAULT_ORDER)


def test_streamdeck_non_utf8_config_falls_back_to_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    f = tmp_path / "config.toml"
    f.write_bytes(b'theme = "\xff\xfe"\n')
    cfg = sd_config.load(f)
    assert cfg.theme == sd_config.Config().theme


def test_streamdeck_unreadable_config_falls_back_to_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    # A directory where the file should be: exists() is true, read fails.
    d = tmp_path / "config.toml"
    d.mkdir()
    cfg = sd_config.load(d)
    assert cfg.keys == list(sd_actions.DEFAULT_ORDER)


@pytest.mark.parametrize("content", [
    'keys = ["pending", "commit"\n',
    b'theme = "\xff\xfe"\n',
], ids=["bad-toml", "non-utf8"])
def test_streamdeck_strict_load_raises_on_a_corrupt_config(monkeypatch, tmp_path, content):
    # A running controller reloads with strict=True so a bad edit keeps the
    # layout it already shows instead of dropping to the defaults.
    _clear_env(monkeypatch)
    f = tmp_path / "config.toml"
    if isinstance(content, bytes):
        f.write_bytes(content)
    else:
        f.write_text(content)
    with pytest.raises((sd_config.tomllib.TOMLDecodeError, UnicodeDecodeError)):
        sd_config.load(f, strict=True)


def test_streamdeck_strict_load_of_a_good_file_matches_the_default_mode(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    f = tmp_path / "config.toml"
    f.write_text('keys = ["pending", "commit"]\nrotation = 90\n')
    assert sd_config.load(f, strict=True) == sd_config.load(f)


def test_streamdeck_valid_config_still_applies(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    f = tmp_path / "config.toml"
    f.write_text('keys = ["pending", "commit"]\nrotation = 90\n')
    cfg = sd_config.load(f)
    assert cfg.keys == ["pending", "commit"]
    assert cfg.rotation == 90
