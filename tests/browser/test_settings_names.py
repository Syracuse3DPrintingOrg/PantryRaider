"""Settings fields have names a screen reader can say, and the decorative
label preview is drawn before anything is typed.

A settings row shows its title beside the control instead of in a <label>;
panes.js points each row's one unnamed field at that title. The check opens
the panes a first-time user visits and asks every visible field for a name.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_UNNAMED = """pane => {
  document.querySelector(`[data-bs-target="#pane-${pane}"]`).click();
  const root = document.getElementById('pane-' + pane);
  return [...root.querySelectorAll('input:not([type=hidden]), select, textarea')]
    .filter(c => c.offsetParent !== null)
    .filter(c => {
      if (c.getAttribute('aria-label')) return false;
      const by = c.getAttribute('aria-labelledby');
      if (by && by.split(/\\s+/).some(id => (document.getElementById(id) || {}).textContent)) return false;
      if ([...(c.labels || [])].some(l => l.textContent.trim())) return false;
      if (c.title || c.placeholder) return false;
      return true;
    })
    .map(c => c.id || c.outerHTML.slice(0, 80));
}"""


@pytest.mark.parametrize("pane", ["security", "backups", "scanning", "appearance", "printing"])
def test_settings_fields_have_names(new_page, pane):
    page = new_page("/setup")
    page.wait_for_selector(f'[data-bs-target="#pane-{pane}"]', state="attached")
    page.wait_for_timeout(300)
    assert page.evaluate(_UNNAMED, pane) == []


def test_decorative_label_preview_is_drawn_on_load(new_page):
    page = new_page("/setup")
    # A function, not a string: the page's CSP refuses string evaluation.
    page.wait_for_function(
        "() => { const i = document.getElementById('decorative-preview-img');"
        " return i && i.complete && i.naturalWidth > 0; }", timeout=5000)
