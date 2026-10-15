"""The grey outline button reads in every theme (FoodAssistant-fy6kj).

``btn-outline-secondary`` is the app's everyday quiet button (Cancel, Edit,
filters, pagers), and in stock Bootstrap its grey sat near 3.5:1 against the
page. Each bundled theme is rendered here the way base.html stacks it (the
Bootstrap or Bootswatch sheet, the theme overlay, then base.css) and the
button's computed colours are checked against the page, a card and the
tertiary panel: text at 4.5:1 or better and the border at 3:1 or better.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "service"))

from app.config import THEMES  # noqa: E402

_STATIC = _ROOT / "service" / "app" / "static"

_MEASURE = r"""
() => {
  const rgb = s => {
    const m = s.match(/rgba?\(([^)]+)\)/);
    const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  // The first opaque background found walking up from an element, so a
  // transparent card body resolves to what is actually painted under it.
  const bgOf = el => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let out = [255, 255, 255];
    for (const c of layers.reverse()) {
      out = out.map((v, i) => v * (1 - c[3]) + c[i] * c[3]);
    }
    return out;
  };
  const res = {};
  for (const id of ['page', 'card', 'tertiary']) {
    const btn = document.querySelector('#' + id + ' .btn-outline-secondary');
    const cs = getComputedStyle(btn);
    res[id] = {
      text: rgb(cs.color), border: rgb(cs.borderTopColor),
      bg: bgOf(btn.parentElement),
    };
  }
  return res;
}
"""


def _lin(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb) -> float:
    r, g, b = (_lin(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg, bg) -> float:
    # A translucent foreground is composited over the background first.
    a = fg[3] if len(fg) > 3 else 1
    fg = [fg[i] * a + bg[i] * (1 - a) for i in range(3)]
    la, lb = _lum(fg), _lum(bg)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _page(theme: dict, key: str) -> str:
    sheet = theme["stylesheet"] or "static/vendor/bootstrap.min.css"
    links = [sheet] + ([theme["overlay"]] if theme["overlay"] else []) + ["static/css/base.css"]
    head = "".join(
        f'<link rel="stylesheet" href="{(_STATIC.parent / href).as_uri()}">' for href in links)
    btn = '<button class="btn btn-outline-secondary">Cancel</button>'
    return (
        f'<!doctype html><html lang="en" data-bs-theme="{theme["mode"]}" '
        f'data-pr-theme="{key}"><head>{head}</head><body>'
        f'<main class="container"><div id="page">{btn}</div>'
        f'<div class="card"><div class="card-body" id="card">{btn}</div></div>'
        f'<div class="bg-body-tertiary p-3" id="tertiary">{btn}</div>'
        f'</main></body></html>')


# Custom builds its colours from the user's own swatches, so there is nothing
# fixed to check.
_KEYS = [k for k in THEMES if k != "custom"]


@pytest.mark.parametrize("key", _KEYS)
def test_outline_secondary_passes_contrast(browser, tmp_path, key):
    html = tmp_path / f"{key}.html"
    html.write_text(_page(THEMES[key], key), encoding="utf-8")
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(html.as_uri(), wait_until="load")
        got = page.evaluate(_MEASURE)
    finally:
        ctx.close()
    fails = []
    for where, m in got.items():
        text, border = _ratio(m["text"], m["bg"]), _ratio(m["border"], m["bg"])
        if text < 4.5 or border < 3.0:
            fails.append((where, round(text, 2), round(border, 2), m))
    assert not fails, (key, fails)
