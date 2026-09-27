// Keyboard navigation for the kiosk web UI.
// Lets a plain USB keyboard drive the top nav when there is no touchscreen
// or Stream Deck attached. Number keys jump to nav tabs, "?" shows help.
(function () {
  'use strict';

  // Guard against double-inclusion.
  if (window.__keyboardNavLoaded) { return; }
  window.__keyboardNavLoaded = true;

  // Returns the visible top-nav anchors in document order. Read live from the
  // DOM so hidden or service-gated tabs are respected automatically.
  function navLinks() {
    var nodes = document.querySelectorAll('.navbar-nav.me-auto .nav-link');
    var links = [];
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      // Only real navigable links with an href, and only if rendered.
      if (el.tagName === 'A' && el.getAttribute('href') && el.offsetParent !== null) {
        links.push(el);
      }
    }
    return links;
  }

  function linkLabel(el) {
    return (el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  // Map a key value to a zero-based nav index. "1".."9" -> 0..8, "0" -> 9.
  function indexForKey(key) {
    if (key === '0') { return 9; }
    if (key >= '1' && key <= '9') { return key.charCodeAt(0) - '1'.charCodeAt(0); }
    return -1;
  }

  function isEditable(el) {
    if (!el) { return false; }
    var tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return true; }
    if (el.isContentEditable) { return true; }
    return false;
  }

  // Help overlay (built on demand, reused after that).
  var overlay = null;

  function buildOverlay() {
    var el = document.createElement('div');
    el.id = 'kbd-nav-help';
    el.style.position = 'fixed';
    el.style.inset = '0';
    el.style.zIndex = '11000';
    el.style.display = 'none';
    el.style.alignItems = 'center';
    el.style.justifyContent = 'center';
    el.style.background = 'rgba(0,0,0,0.6)';
    el.className = 'p-3';
    el.addEventListener('click', function (ev) {
      if (ev.target === el) { hideOverlay(); }
    });
    document.body.appendChild(el);
    return el;
  }

  function renderOverlay() {
    if (!overlay) { overlay = buildOverlay(); }
    var links = navLinks();
    var rows = '';
    for (var i = 0; i < links.length && i < 10; i++) {
      var num = (i === 9) ? '0' : String(i + 1);
      var label = linkLabel(links[i]) || links[i].getAttribute('href');
      rows += '<li class="d-flex align-items-center mb-2">' +
        '<kbd class="me-3">' + num + '</kbd>' +
        '<span>' + escapeHtml(label) + '</span></li>';
    }
    overlay.innerHTML =
      '<div class="card bg-body-tertiary shadow" style="max-width:360px;width:100%">' +
        '<div class="card-header d-flex justify-content-between align-items-center">' +
          '<strong><i class="bi bi-keyboard me-2"></i>Keyboard shortcuts</strong>' +
          '<button type="button" class="btn-close" aria-label="Close" id="kbd-nav-close"></button>' +
        '</div>' +
        '<div class="card-body">' +
          '<ul class="list-unstyled mb-2">' + rows + '</ul>' +
          '<p class="small text-muted mb-0">Press <kbd>?</kbd> or <kbd>Esc</kbd> to close.</p>' +
        '</div>' +
      '</div>';
    var closeBtn = document.getElementById('kbd-nav-close');
    if (closeBtn) { closeBtn.addEventListener('click', hideOverlay); }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function overlayVisible() {
    return overlay && overlay.style.display !== 'none';
  }

  function showOverlay() {
    renderOverlay();
    overlay.style.display = 'flex';
  }

  function hideOverlay() {
    if (overlay) { overlay.style.display = 'none'; }
  }

  function toggleOverlay() {
    if (overlayVisible()) { hideOverlay(); } else { showOverlay(); }
  }

  // Exposed so the navbar keyboard-icon hint can open the help overlay too.
  window.__kbdNavHelp = toggleOverlay;

  // A USB HID barcode scanner types its code into the page as a fast keystroke
  // burst. Navigating on the first digit hijacked every scan to whatever tab
  // owned that number (usually Inventory in slot 1, sometimes Weather),
  // regardless of scanner mode (FoodAssistant-lth6). So a digit shortcut only
  // navigates after a short quiet delay, and ANY further keystroke inside that
  // window cancels it: scanner keystrokes arrive within a few milliseconds of
  // each other (the global barcode capture in base.html then consumes the
  // burst), while a human shortcut press is a single keystroke, for which the
  // delay is imperceptible. A digit that itself arrives hot on the heels of
  // another keystroke is part of a burst, never a shortcut.
  var pendingNav = null;
  var lastKeyAt = 0;
  var NAV_DELAY_MS = 200;
  var BURST_GAP_MS = 250;

  document.addEventListener('keydown', function (ev) {
    var sinceLast = Date.now() - lastKeyAt;
    lastKeyAt = Date.now();
    if (pendingNav) { clearTimeout(pendingNav); pendingNav = null; }

    // Never interfere with typing or browser shortcuts.
    if (ev.ctrlKey || ev.altKey || ev.metaKey) { return; }
    if (isEditable(ev.target)) { return; }

    if (ev.key === 'Escape') {
      if (overlayVisible()) { ev.preventDefault(); hideOverlay(); }
      return;
    }

    // "?" is Shift+/ on most layouts; match the resolved character.
    if (ev.key === '?') {
      ev.preventDefault();
      toggleOverlay();
      return;
    }

    var idx = indexForKey(ev.key);
    if (idx < 0) { return; }
    if (sinceLast < BURST_GAP_MS) { return; }
    var links = navLinks();
    if (idx >= links.length) { return; }
    hideOverlay();
    var target = links[idx].href || links[idx].getAttribute('href');
    if (target) {
      pendingNav = setTimeout(function () { window.location.href = target; }, NAV_DELAY_MS);
    }
  });
})();
