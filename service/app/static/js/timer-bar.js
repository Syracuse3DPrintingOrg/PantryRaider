/*
 * Timer status bar for a kitchen display.
 *
 * A Bandit with a key pad shows a running timer on its keys: a bar that
 * drains as the time runs down, and a flash when it is up. A display with no
 * key pad (a Bandit without one, a tablet, an Echo Show) gets the same thing
 * on screen: a thin bar across the top that drains with the soonest timer,
 * with its name and time left, on every page including the start page, in
 * the theme's own colors. It flashes in the timer color from Settings when a
 * timer has finished. A tap opens the timers.
 *
 * Only on a kitchen display or a tablet signed in as a screen, never in an
 * ordinary browser (the floating chips cover those), and the server leaves it
 * out when this device has a key pad. Plain ES5: old tablet web views run it.
 */
(function () {
  'use strict';
  var html = document.documentElement;
  var cfg = document.getElementById('timer-bar-config');
  if (!cfg) return;
  var kiosk = false;
  try { kiosk = localStorage.getItem('kioskMode') === 'true'; } catch (e) { }
  if (!kiosk && !html.getAttribute('data-screen-id')) return;

  var COLOR = cfg.getAttribute('data-color') || '#FF0000';
  // A tablet signed in as a screen is read from across the kitchen (an Echo
  // Show 8 lays out 963 x 594 CSS px, each one small on the glass), so its
  // bar and text are larger than on this install's own display.
  var TABLET = !!html.getAttribute('data-screen-id');
  var HEIGHT = TABLET ? 38 : 28;
  var TEXT_PX = TABLET ? 20 : 15;
  var POLL_MS = 5000;
  var timers = [];
  var skew = 0;
  var bar = null, fill = null, text = null;
  var savedInset = null;

  var style = document.createElement('style');
  // The theme's own accent color, deepening toward the left. The finished-timer flash
  // uses the timer color from Settings, so "time is up" reads apart from
  // "counting down" in any theme.
  var ACCENT = 'var(--bs-primary, #F2006E)';
  style.textContent =
    '.tb{position:fixed;top:0;left:0;right:0;height:' + HEIGHT + 'px;z-index:2040;overflow:hidden;cursor:pointer;' +
    'background:#15171b;background:var(--bs-tertiary-bg, #15171b);' +
    'border-bottom:1px solid var(--bs-border-color, #2a2a35);' +
    'font-family:inherit}' +
    '.tb-fill{position:absolute;top:0;left:0;bottom:0;width:0;border-radius:0 ' + HEIGHT + 'px ' + HEIGHT + 'px 0;' +
    'background:#F2006E;background:' + ACCENT + ';' +
    'background:linear-gradient(90deg,color-mix(in srgb,' + ACCENT + ' 55%,#000) 0%,' + ACCENT + ' 100%);' +
    'box-shadow:0 0 18px ' + ACCENT + ';transition:width 1s linear}' +
    '.tb-text{position:absolute;top:0;left:0;right:0;line-height:' + HEIGHT + 'px;text-align:center;' +
    'color:#fff;font-size:' + TEXT_PX + 'px;font-weight:700;letter-spacing:.02em;white-space:nowrap;overflow:hidden;' +
    'text-overflow:ellipsis;text-shadow:0 1px 3px rgba(0,0,0,.75);padding:0 64px}' +
    '.tb.done .tb-fill{width:100%!important;border-radius:0;background:' + COLOR + ';box-shadow:none;' +
    '-webkit-animation:tb-flash .7s infinite alternate;animation:tb-flash .7s infinite alternate}' +
    '@-webkit-keyframes tb-flash{from{opacity:.25}to{opacity:1}}' +
    '@keyframes tb-flash{from{opacity:.25}to{opacity:1}}';
  document.head.appendChild(style);

  function now() { return Date.now() / 1000 + skew; }
  function left(t) { return Math.max(0, t.deadline_epoch - now()); }
  function clock(sec) {
    sec = Math.ceil(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  // The bar takes the top strip of the screen, so the page moves down by the
  // same safe-area inset a panel with a hidden rim uses. A screen that already
  // keeps that strip clear (a kiosk app's own close button) needs no more.
  var paddedBody = false;
  function reserve(on) {
    var st = html.style;
    var body = document.body;
    if (on) {
      if (savedInset !== null) return;
      savedInset = st.getPropertyValue('--kiosk-inset-top') || '';
      var have = parseFloat(savedInset) || 0;
      var zoom = parseFloat(html.getAttribute('data-ui-scale') || '1') || 1;
      var want = HEIGHT / zoom;
      if (have >= want) return;
      var before = parseFloat(window.getComputedStyle(body).paddingTop) || 0;
      st.setProperty('--kiosk-inset-top', want.toFixed(2) + 'px');
      // The bar across the top of a page moves with that inset; on some pages
      // the content below it does not, and would sit under the bar. Where the
      // content did not move, move it.
      var after = parseFloat(window.getComputedStyle(body).paddingTop) || 0;
      if (after - before < (want - have) - 1) {
        body.style.paddingTop = (before + want - have) + 'px';
        paddedBody = true;
      }
    } else if (savedInset !== null) {
      if (savedInset) st.setProperty('--kiosk-inset-top', savedInset);
      else st.removeProperty('--kiosk-inset-top');
      savedInset = null;
      if (paddedBody) { body.style.paddingTop = ''; paddedBody = false; }
    }
  }

  function draw() {
    var running = [], done = [], i;
    for (i = 0; i < timers.length; i++) {
      // Done when the server says so (a page opened while one rings), or
      // when its deadline has passed on this clock.
      if (timers[i].expired !== true && left(timers[i]) > 0) running.push(timers[i]); else done.push(timers[i]);
    }
    if (!running.length && !done.length) {
      if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
      bar = null;
      reserve(false);
      return;
    }
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'tb';
      fill = document.createElement('div'); fill.className = 'tb-fill';
      text = document.createElement('div'); text.className = 'tb-text';
      bar.appendChild(fill); bar.appendChild(text);
      bar.addEventListener('click', function () { window.location.assign('ui/timers'); });
      document.body.appendChild(bar);
      reserve(true);
    }
    if (done.length) {
      bar.className = 'tb done';
      text.textContent = done[0].label + ' is done' + (done.length + running.length > 1
        ? ' (+' + (done.length + running.length - 1) + ' more)' : '');
      return;
    }
    running.sort(function (a, b) { return left(a) - left(b); });
    var t = running[0];
    bar.className = 'tb';
    var share = t.total_seconds > 0 ? left(t) / t.total_seconds : 1;
    fill.style.width = (Math.max(0, Math.min(1, share)) * 100).toFixed(2) + '%';
    text.textContent = t.label + '  ' + clock(left(t)) +
      (running.length > 1 ? '  (+' + (running.length - 1) + ' more)' : '');
  }

  var busy = false;
  function poll() {
    if (busy || document.hidden) return;
    busy = true;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', 'timers', true);
    xhr.timeout = 8000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      busy = false;
      if (xhr.status !== 200) return;
      try {
        var list = JSON.parse(xhr.responseText).timers || [];
        for (var i = 0; i < list.length; i++) {
          if (list[i].running && typeof list[i].remaining_seconds === 'number') {
            // The server's clock, so a tablet with a wrong clock still counts right.
            skew = (list[i].deadline_epoch - list[i].remaining_seconds) - Date.now() / 1000;
            break;
          }
        }
        timers = list;
        draw();
      } catch (e) { /* keep what we have */ }
    };
    try { xhr.send(null); } catch (e) { busy = false; }
  }
  function pollLoop() { poll(); setTimeout(pollLoop, POLL_MS); }
  function tickLoop() { if (timers.length) draw(); setTimeout(tickLoop, 1000); }
  function begin() { pollLoop(); tickLoop(); }
  if (document.body) begin();
  else document.addEventListener('DOMContentLoaded', begin);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
