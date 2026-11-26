/*
 * Status icons for a kitchen display.
 *
 * A short row of small, see-through icons in a top corner, each one there
 * only while something in the kitchen is true: a thermometer is reporting,
 * the fridge door is open, the oven is on. The list and the conditions are
 * set in Settings, Home Assistant; this asks /status-icons which ones show
 * right now. A tap on an icon says what it is.
 *
 * Only on a kitchen display or a tablet signed in as a screen, never in an
 * ordinary browser. They sit in the page's top bar beside its own buttons
 * (the navigation bar, or a tablet page's bar through the element with id
 * sc-status-icons), or just below it when it has no room. A page with no bar
 * (the start page) gets them at the very top, below the timer bar and the
 * corner a kiosk app keeps for its close button (both reserve the top
 * through --kiosk-inset-top). The colors are the theme's own.
 *
 * Plain ES5 with XMLHttpRequest: old tablet web views run it.
 */
(function () {
  'use strict';
  var html = document.documentElement;
  var cfg = document.getElementById('status-icons-config');
  if (!cfg) return;
  var inline = document.getElementById('sc-status-icons');
  var kiosk = false;
  try { kiosk = localStorage.getItem('kioskMode') === 'true'; } catch (e) { }
  if (!kiosk && !inline && !html.getAttribute('data-screen-id')) return;

  var POLL_MS = 5000;
  var POSITION = cfg.getAttribute('data-position') === 'top_left' ? 'top_left' : 'top_right';
  // Room to leave beside the corner for a page's own corner button.
  var GAP = parseInt(cfg.getAttribute('data-gap') || '0', 10) || 0;
  var SIZE = 34;

  var SVG = {
    thermometer: '<path d="M10 4a2 2 0 0 1 4 0v9.3a4 4 0 1 1-4 0z"/><path d="M12 9v7"/>',
    fridge: '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M6 9h12M9.5 5v2M9.5 12v4"/>',
    oven: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 8.5h18"/>' +
      '<rect x="7" y="11.5" width="10" height="5.5" rx="1"/><path d="M7 6.2h.01M10.5 6.2h.01"/>',
    pot: '<path d="M4 11h16v6a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3z"/><path d="M2 11h20"/>' +
      '<path d="M9 3c-1 1.3 1 2.3 0 4.5M15 3c-1 1.3 1 2.3 0 4.5"/>',
    plug: '<path d="M9 2v5M15 2v5"/><path d="M6 7h12v4a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
    door: '<path d="M6 21V3h12v18"/><path d="M3 21h18"/><path d="M14.5 12h.01"/>',
    fan: '<circle cx="12" cy="12" r="1.8"/>' +
      '<path d="M12 10.2C11 6.5 11.5 3 14 3c2.6 0 2.3 4-2 7.2"/>' +
      '<path d="M13.6 12.9c3.7 1 6.4 3.2 5.2 5.4-1.3 2.2-4.6.1-5.2-5.4"/>' +
      '<path d="M10.4 12.9c-2.7 2.7-6 4-7.2 1.8-1.3-2.2 2.2-4 7.2-1.8"/>',
    generic: '<circle cx="12" cy="12" r="5"/>'
  };

  var ACCENT = inline ? '#F2006E' : 'var(--bs-primary, #F2006E)';
  var SURFACE = inline ? '#1c1f25' : 'var(--bs-body-bg, #14161a)';
  var side = POSITION === 'top_left' ? 'left' : 'right';
  var style = document.createElement('style');
  style.textContent =
    '.si-row{display:-webkit-flex;display:flex;-webkit-align-items:center;align-items:center;' +
    'pointer-events:none;font-family:inherit}' +
    '.si-row.si-fixed{position:fixed;z-index:2030}' +
    '.si-row.si-bar{margin:0 8px;-webkit-flex-shrink:0;flex-shrink:0}' +
    '.si-ic{position:relative;width:' + SIZE + 'px;height:' + SIZE + 'px;margin:0 3px;border-radius:50%;' +
    'box-sizing:border-box;pointer-events:auto;cursor:pointer;opacity:.78;' +
    'color:' + ACCENT + ';background:rgba(20,22,26,.45);' +
    'background:color-mix(in srgb,' + SURFACE + ' 45%,transparent);' +
    'border:1px solid rgba(242,0,110,.45);border:1px solid color-mix(in srgb,' + ACCENT + ' 45%,transparent);' +
    '-webkit-transition:opacity .3s;transition:opacity .3s}' +
    '.si-ic svg{display:block;width:20px;height:20px;margin:6px;fill:none;stroke:currentColor;' +
    'stroke-width:2;stroke-linecap:round;stroke-linejoin:round}' +
    '.si-ic.si-new{-webkit-animation:si-in .4s ease-out;animation:si-in .4s ease-out}' +
    '@-webkit-keyframes si-in{from{opacity:0;-webkit-transform:scale(.6)}to{opacity:.78;-webkit-transform:none}}' +
    '@keyframes si-in{from{opacity:0;transform:scale(.6)}to{opacity:.78;transform:none}}' +
    '.si-tip{position:absolute;top:' + (SIZE + 6) + 'px;' + side + ':0;white-space:nowrap;' +
    'padding:4px 10px;border-radius:8px;font-size:14px;font-weight:600;line-height:1.3;' +
    'color:#fff;color:var(--bs-body-color, #fff);background:#1c1f25;background:var(--bs-body-bg, #1c1f25);' +
    'border:1px solid ' + ACCENT + ';pointer-events:none}' +
    '@media (prefers-reduced-motion: reduce){.si-ic.si-new{-webkit-animation:none;animation:none}}';
  document.head.appendChild(style);

  var row = null;
  var shown = {};
  var tipTimer = null;

  function label(item) {
    return item.detail ? item.name + ': ' + item.detail : item.name;
  }

  function showTip(el, text) {
    var old = row && row.querySelector('.si-tip');
    if (old && old.parentNode) old.parentNode.removeChild(old);
    if (tipTimer) clearTimeout(tipTimer);
    var tip = document.createElement('div');
    tip.className = 'si-tip';
    tip.textContent = text;
    el.appendChild(tip);
    tipTimer = setTimeout(function () {
      if (tip.parentNode) tip.parentNode.removeChild(tip);
    }, 3500);
  }

  // Where the row goes. A page with a top bar (the navigation bar, or a
  // tablet page's own bar) takes the icons into the bar, beside its own
  // buttons, so they never cover one. When the bar is hidden or has no room,
  // the row floats just below it; with no bar at all it floats at the top,
  // below the timer bar and a kiosk app's corner (--kiosk-inset-top).
  var nav = document.querySelector('nav.navbar.fixed-top');
  var navBox = nav ? nav.querySelector('.container-fluid') : null;
  var topBar = inline ? inline.parentNode : nav;

  function visible(el) {
    return !!el && el.offsetHeight > 0 && el.getClientRects().length > 0 &&
      window.getComputedStyle(el).visibility !== 'hidden';
  }

  function intoBar() {
    if (inline) {
      if (row.parentNode !== inline) inline.appendChild(row);
      return;
    }
    if (side === 'left') {
      var brand = navBox.querySelector('.navbar-brand');
      var after = brand ? brand.nextSibling : navBox.firstChild;
      if (row.parentNode !== navBox || row.previousSibling !== brand) navBox.insertBefore(row, after);
    } else {
      var toggler = navBox.querySelector('.navbar-toggler');
      if (row.parentNode !== navBox || row.nextSibling !== toggler) navBox.insertBefore(row, toggler);
    }
  }

  function fits(box) {
    var r = row.getBoundingClientRect(), b = box.getBoundingClientRect();
    var cs = window.getComputedStyle(box);
    var leftEdge = b.left + (parseFloat(cs.paddingLeft) || 0);
    var rightEdge = b.right - (parseFloat(cs.paddingRight) || 0);
    return r.width > 0 && r.left >= leftEdge - 1 && r.right <= rightEdge + 1;
  }

  function floatAt(topPx, useInset) {
    if (row.parentNode !== document.body) document.body.appendChild(row);
    row.className = 'si-row si-fixed';
    var off = 8 + GAP;
    // A tablet in the whole interface has its own Exit button in the top
    // right corner (screen-app.js), wider than the start page's own button.
    var exit = side === 'right' ? document.querySelector('.sa-exit') : null;
    if (exit && exit.offsetWidth) off = exit.offsetWidth + 16;
    // The open-install notice pushes a page without a bar down by its height.
    var banner = useInset && html.className.indexOf('pr-access-banner-shown') >= 0
      ? ' + var(--pr-access-banner-h, 0px)' : '';
    row.style.top = useInset ? 'calc(var(--kiosk-inset-top, 0px)' + banner + ' + ' + topPx + 'px)' : topPx + 'px';
    row.style[side] = useInset ? 'calc(var(--kiosk-inset-' + side + ', 0px) + ' + off + 'px)' : off + 'px';
  }

  function place() {
    if (!row) return;
    var barBox = inline ? topBar : navBox;
    if (topBar && barBox && visible(topBar)) {
      row.className = 'si-row si-bar';
      row.style.top = '';
      row.style[side] = '';
      intoBar();
      if (fits(barBox)) return;
      // No room in the bar: float just below it instead.
      floatAt(topBar.offsetTop + topBar.offsetHeight + 6, false);
      return;
    }
    floatAt(6, true);
  }

  function ensureRow() {
    if (row) return row;
    row = document.createElement('div');
    row.className = 'si-row';
    row.setAttribute('role', 'status');
    row.setAttribute('aria-live', 'polite');
    document.body.appendChild(row);
    return row;
  }

  function draw(items) {
    var keep = {}, i, item, el;
    if (!items.length) {
      if (row && row.parentNode) row.parentNode.removeChild(row);
      row = null;
      shown = {};
      return;
    }
    ensureRow();
    for (i = 0; i < items.length; i++) {
      item = items[i];
      keep[item.id] = true;
      el = shown[item.id];
      if (!el) {
        el = document.createElement('div');
        el.className = 'si-ic si-new';
        el.setAttribute('data-id', item.id);
        el.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
          (SVG[item.icon] || SVG.generic) + '</svg>';
        el.addEventListener('click', (function (node) {
          return function (ev) {
            if (ev && ev.stopPropagation) ev.stopPropagation();
            showTip(node, node.getAttribute('aria-label') || '');
          };
        })(el));
        shown[item.id] = el;
      }
      el.setAttribute('title', label(item));
      el.setAttribute('aria-label', label(item));
      row.appendChild(el);   // appending again keeps the saved order
    }
    for (var id in shown) {
      if (shown.hasOwnProperty(id) && !keep[id]) {
        if (shown[id].parentNode) shown[id].parentNode.removeChild(shown[id]);
        delete shown[id];
      }
    }
    place();
  }

  var busy = false, gone = false, current = null;
  function poll() {
    if (busy || gone || document.hidden) return;
    busy = true;
    var xhr = new XMLHttpRequest();
    current = xhr;
    xhr.open('GET', 'status-icons', true);
    xhr.timeout = 8000;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      busy = false;
      current = null;
      if (xhr.status !== 200) return;
      try {
        var data = JSON.parse(xhr.responseText) || {};
        draw(data.enabled && data.items ? data.items : []);
      } catch (e) { /* keep what is showing */ }
    };
    try { xhr.send(null); } catch (e) { busy = false; current = null; }
  }
  function loop() {
    if (gone) return;
    poll();
    setTimeout(loop, POLL_MS);
  }
  if (window.addEventListener) {
    // Leaving the page: drop the request so it never holds up the next one.
    window.addEventListener('pagehide', function () {
      gone = true;
      if (current) { try { current.abort(); } catch (e) { } }
    });
    window.addEventListener('pageshow', function (ev) {
      if (ev.persisted && gone) { gone = false; busy = false; loop(); }
    });
  }
  function begin() { loop(); }
  if (document.body) begin();
  else document.addEventListener('DOMContentLoaded', begin);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  window.addEventListener('resize', function () { place(); });
})();
