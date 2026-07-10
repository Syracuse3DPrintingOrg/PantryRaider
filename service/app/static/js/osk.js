// On-screen keyboard for kiosk touchscreens (FoodAssistant-wo9j).
//
// A wall-mounted panel has no physical keyboard, so text fields (custom timer
// names, manual barcode entry, shopping quick-add, search boxes, settings
// fields) were unusable there. This keyboard docks to the bottom of the
// screen and slides up whenever a text-like input gains focus, IN KIOSK MODE
// ONLY (localStorage kioskMode, the same gate the screensaver and intro use).
// The per-device "On-screen keyboard" setting (#osk-config data-enabled,
// default on) turns it off for kiosks with an attached keyboard.
//
// Covered inputs: input[type=text|search|number|email|url|password] and
// textarea; a number input gets a digits-only pad. Characters are inserted at
// the caret via document.execCommand('insertText') (which fires the input
// event natively) with a value-splice + synthetic input event fallback, so
// the app's vanilla-JS listeners react exactly as they would to real typing.
// The Enter key mirrors a real Enter keydown on the input first (existing
// keydown handlers run and may preventDefault), then submits the input's
// form; on a textarea it inserts a newline instead.
//
// Interplay: the keyboard sits BELOW the screensaver (z 2147483000) and the
// boot intro (z 2147483100) and hides the moment the saver shows
// (body.ss-active). Taps on its keys bubble to the window like any touch, so
// the screensaver idle counter and the kiosk-idle wake reporter both count
// them as activity. While open, a bottom spacer keeps the focused input
// scrollable above the keys.
(function () {
  var kiosk = false;
  try {
    kiosk = localStorage.getItem('kioskMode') === 'true';
  } catch (e) { /* no storage: never a kiosk */ }
  if (!kiosk) return;

  // Per-device setting: on by default in kiosk mode; the config div (rendered
  // from osk_enabled) turns it off for panels with an attached keyboard.
  var cfg = document.getElementById('osk-config');
  if (cfg && cfg.getAttribute('data-enabled') === 'false') return;

  // Below the screensaver overlay (2147483000) and the intro (2147483100),
  // above everything the page itself draws.
  var Z_INDEX = 2147482000;

  var TEXT_TYPES = ['text', 'search', 'number', 'email', 'url', 'password'];

  // Letters row by row; digits ride on top so no layer switch is needed.
  var ROWS_TEXT = [
    ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
    ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
    ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
    ['SHIFT', 'z', 'x', 'c', 'v', 'b', 'n', 'm', 'BKSP'],
    ['DONE', '@', ',', 'SPACE', '.', '-', 'ENTER'],
  ];
  // Digits-only pad for input[type=number] (plus decimal point and minus).
  var ROWS_NUMBER = [
    ['1', '2', '3'],
    ['4', '5', '6'],
    ['7', '8', '9'],
    ['.', '0', 'BKSP'],
    ['DONE', '-', 'ENTER'],
  ];
  // Shifted faces: digits become the common symbols, punctuation swaps to a
  // second set. Letters just uppercase.
  var SHIFT_MAP = {
    '1': '!', '2': '?', '3': '#', '4': '$', '5': '%',
    '6': '&', '7': '*', '8': '(', '9': ')', '0': '/',
    '@': "'", ',': ';', '.': ':', '-': '_',
  };

  var kb = null;          // the keyboard element, built lazily on first use
  var shim = null;        // bottom spacer so the input can scroll above the keys
  var target = null;      // the input the keyboard is typing into
  var layout = '';        // 'text' or 'number', to skip pointless rebuilds
  var shift = 0;          // 0 off, 1 one-shot, 2 caps lock
  var charKeys = [];      // keys whose face changes with shift

  function isEditable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.getAttribute && el.getAttribute('data-osk') === 'off') return false;
    var tag = el.tagName;
    if (tag === 'TEXTAREA') return true;
    if (tag !== 'INPUT') return false;
    var type = (el.getAttribute('type') || 'text').toLowerCase();
    return TEXT_TYPES.indexOf(type) !== -1;
  }

  function faceFor(ch) {
    if (shift === 0) return ch;
    return SHIFT_MAP[ch] || ch.toUpperCase();
  }

  function refreshFaces() {
    for (var i = 0; i < charKeys.length; i++) {
      var k = charKeys[i];
      k.textContent = faceFor(k.getAttribute('data-osk-char'));
    }
    if (kb) {
      var sk = kb.querySelector('[data-osk-action="SHIFT"]');
      if (sk) sk.classList.toggle('pr-osk-shifted', shift > 0);
    }
  }

  function ensureStyle() {
    if (document.getElementById('pr-osk-style')) return;
    var style = document.createElement('style');
    style.id = 'pr-osk-style';
    style.textContent =
      '#pr-osk{position:fixed;left:0;right:0;bottom:0;z-index:' + Z_INDEX + ';' +
      'background:#181a20;border-top:1px solid rgba(255,255,255,0.14);' +
      'padding:6px 6px calc(6px + env(safe-area-inset-bottom,0px));' +
      'box-shadow:0 -6px 18px rgba(0,0,0,0.5);' +
      'transform:translateY(100%);transition:transform 0.18s ease;' +
      'touch-action:manipulation;user-select:none;-webkit-user-select:none;}' +
      '#pr-osk.pr-osk-open{transform:translateY(0);}' +
      '#pr-osk .pr-osk-rows{max-width:900px;margin:0 auto;}' +
      '#pr-osk.pr-osk-number .pr-osk-rows{max-width:420px;}' +
      '#pr-osk .pr-osk-row{display:flex;gap:6px;margin-top:6px;}' +
      '#pr-osk .pr-osk-row:first-child{margin-top:0;}' +
      // kiosk.css touch-target convention: nothing tappable under 48px.
      '#pr-osk button{flex:1 1 0;min-width:48px;min-height:52px;border-radius:8px;' +
      'border:1px solid rgba(255,255,255,0.12);background:#2a2e36;color:#e8eaed;' +
      'font-size:1.25rem;line-height:1;padding:0;font-family:inherit;}' +
      '#pr-osk button:active{background:#3d434e;}' +
      '#pr-osk .pr-osk-special{flex-grow:1.6;background:#20232a;font-size:1rem;color:#b8bcc4;}' +
      '#pr-osk .pr-osk-space{flex-grow:4;}' +
      '#pr-osk .pr-osk-enter{background:#F2006E;border-color:#F2006E;color:#fff;}' +
      '#pr-osk .pr-osk-shifted{background:#3d434e;color:#fff;border-color:rgba(255,255,255,0.4);}' +
      '#pr-osk-shim{width:100%;}';
    document.head.appendChild(style);
  }

  function buildKeyboard(rows, numeric) {
    charKeys = [];
    var box = document.createElement('div');
    box.id = 'pr-osk';
    if (numeric) box.className = 'pr-osk-number';
    var inner = document.createElement('div');
    inner.className = 'pr-osk-rows';
    for (var r = 0; r < rows.length; r++) {
      var row = document.createElement('div');
      row.className = 'pr-osk-row';
      for (var c = 0; c < rows[r].length; c++) {
        var def = rows[r][c];
        var key = document.createElement('button');
        key.type = 'button';
        key.tabIndex = -1;
        if (def === 'SHIFT' || def === 'BKSP' || def === 'DONE' ||
            def === 'ENTER' || def === 'SPACE') {
          key.setAttribute('data-osk-action', def);
          if (def === 'SPACE') {
            key.className = 'pr-osk-space';
            key.textContent = ' ';
            key.setAttribute('aria-label', 'Space');
          } else {
            key.className = 'pr-osk-special';
            if (def === 'ENTER') key.className += ' pr-osk-enter';
            key.textContent = { SHIFT: '⇧', BKSP: '⌫',
                                DONE: '▼ hide', ENTER: '↵' }[def] || '';
            if (def === 'SHIFT') key.setAttribute('aria-label', 'Shift');
            if (def === 'BKSP') key.setAttribute('aria-label', 'Backspace');
            if (def === 'ENTER') key.setAttribute('aria-label', 'Enter');
          }
        } else {
          key.setAttribute('data-osk-char', def);
          key.textContent = def;
          charKeys.push(key);
        }
        row.appendChild(key);
      }
      inner.appendChild(row);
    }
    box.appendChild(inner);
    // pointerdown, prevented, so a key tap never steals focus from the input;
    // the event still bubbles to the window, where the screensaver idle
    // counter and the kiosk wake reporter count it as activity.
    box.addEventListener('pointerdown', onKeyDown);
    box.addEventListener('mousedown', function (e) { e.preventDefault(); });
    return box;
  }

  // -- typing into the target ------------------------------------------------

  function fireInput(el) {
    var ev;
    try {
      ev = new Event('input', { bubbles: true });
    } catch (e) {
      ev = document.createEvent('Event');
      ev.initEvent('input', true, false);
    }
    el.dispatchEvent(ev);
  }

  function insertText(el, text) {
    el.focus();
    // number inputs have no caret API (selectionStart throws), so they are
    // append-only; everything else tries execCommand first (it inserts at the
    // caret, respects selections, and fires the input event natively).
    if (el.type === 'number') {
      el.value = el.value + text;
      fireInput(el);
      return;
    }
    var ok = false;
    try { ok = document.execCommand('insertText', false, text); } catch (e) { /* fall through */ }
    if (ok) return;
    var s = el.selectionStart, e2 = el.selectionEnd;
    if (s == null) { s = e2 = el.value.length; }
    el.value = el.value.slice(0, s) + text + el.value.slice(e2);
    try { el.selectionStart = el.selectionEnd = s + text.length; } catch (e) { /* readonly caret */ }
    fireInput(el);
  }

  function backspace(el) {
    el.focus();
    if (el.type === 'number') {
      if (!el.value) return;
      el.value = el.value.slice(0, -1);
      fireInput(el);
      return;
    }
    var ok = false;
    try { ok = document.execCommand('delete', false, null); } catch (e) { /* fall through */ }
    if (ok) return;
    var s = el.selectionStart, e2 = el.selectionEnd;
    if (s == null) { s = e2 = el.value.length; }
    if (s === e2) {
      if (s === 0) return;
      s -= 1;
    }
    el.value = el.value.slice(0, s) + el.value.slice(e2);
    try { el.selectionStart = el.selectionEnd = s; } catch (e) { /* readonly caret */ }
    fireInput(el);
  }

  function keyEvent(type, key) {
    var ev;
    try {
      ev = new KeyboardEvent(type, { key: key, code: key, bubbles: true, cancelable: true });
    } catch (e) {
      ev = document.createEvent('Event');
      ev.initEvent(type, true, true);
      ev.key = key;
    }
    // The constructor ignores keyCode/which; some handlers still read them.
    try {
      Object.defineProperty(ev, 'keyCode', { get: function () { return 13; } });
      Object.defineProperty(ev, 'which', { get: function () { return 13; } });
    } catch (e) { /* keep key only */ }
    return ev;
  }

  // Mirror a real Enter press: keydown first (existing handlers fire and may
  // preventDefault, e.g. a barcode field that consumes Enter itself), then
  // the default action, a newline in a textarea or the form submit.
  function pressEnter(el) {
    el.focus();
    var proceed = el.dispatchEvent(keyEvent('keydown', 'Enter'));
    el.dispatchEvent(keyEvent('keyup', 'Enter'));
    if (!proceed) return;
    if (el.tagName === 'TEXTAREA') { insertText(el, '\n'); return; }
    var form = el.form;
    if (form) {
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else form.submit();
    }
  }

  function onKeyDown(e) {
    // Keep the focus (and the caret) in the input the whole time.
    e.preventDefault();
    var key = e.target && e.target.closest ? e.target.closest('button') : null;
    if (!key || !target) return;
    var action = key.getAttribute('data-osk-action');
    if (action === 'SHIFT') {
      shift = (shift + 1) % 3;  // off -> one-shot -> caps lock -> off
      refreshFaces();
      return;
    }
    if (action === 'DONE') { var t = target; hide(); if (t) t.blur(); return; }
    if (action === 'BKSP') { backspace(target); return; }
    if (action === 'ENTER') { pressEnter(target); return; }
    if (action === 'SPACE') { insertText(target, ' '); return; }
    var ch = key.getAttribute('data-osk-char');
    if (!ch) return;
    insertText(target, faceFor(ch));
    if (shift === 1) { shift = 0; refreshFaces(); }  // one-shot shift spends itself
  }

  // -- show / hide -----------------------------------------------------------

  // The layout-pixel viewport height (visual height divided by the kiosk
  // interface scale's zoom), the space getBoundingClientRect works in; the
  // same math screensaver.js uses (FoodAssistant-vf4f).
  function layoutHeight() {
    var z = 1;
    try {
      z = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
    } catch (e) { /* keep 1 */ }
    return window.innerHeight / z;
  }

  function showFor(el) {
    var wanted = (el.tagName === 'INPUT' && el.type === 'number') ? 'number' : 'text';
    ensureStyle();
    if (kb && layout !== wanted) { removeKeyboard(); }
    if (!kb) {
      layout = wanted;
      shift = 0;
      kb = buildKeyboard(wanted === 'number' ? ROWS_NUMBER : ROWS_TEXT,
                         wanted === 'number');
      document.body.appendChild(kb);
      shim = document.createElement('div');
      shim.id = 'pr-osk-shim';
      document.body.appendChild(shim);
      kb.getBoundingClientRect();  // commit the closed frame so the slide runs
    }
    target = el;
    kb.classList.add('pr-osk-open');
    var kbH = kb.offsetHeight;
    shim.style.height = kbH + 'px';
    // Bring the focused input above the keys: nearest scroll first, then nudge
    // the page by any remaining overlap with the keyboard band.
    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) { /* older API */ }
    var r = el.getBoundingClientRect();
    var overlap = r.bottom - (layoutHeight() - kbH);
    if (overlap > 0) window.scrollBy(0, overlap + 12);
  }

  function removeKeyboard() {
    if (kb && kb.parentNode) kb.parentNode.removeChild(kb);
    if (shim && shim.parentNode) shim.parentNode.removeChild(shim);
    kb = null;
    shim = null;
    charKeys = [];
    layout = '';
  }

  function hide() {
    target = null;
    if (!kb) return;
    kb.classList.remove('pr-osk-open');
    if (shim) shim.style.height = '0px';
  }

  function init() {
    // Focus moving into a text-like input opens the keyboard; focus landing
    // anywhere else closes it. Key taps never move focus (pointerdown is
    // prevented), so the keyboard survives its own presses.
    document.addEventListener('focusin', function (e) {
      if (isEditable(e.target)) showFor(e.target);
      else hide();
    }, true);
    document.addEventListener('focusout', function () {
      setTimeout(function () {
        if (!isEditable(document.activeElement)) hide();
      }, 0);
    }, true);
    // Never sit over the screensaver: the saver tags body.ss-active while it
    // is up, so watch for it and duck out (the saver's z-index also wins).
    var mo = new MutationObserver(function () {
      if (document.body.classList.contains('ss-active')) hide();
    });
    mo.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
