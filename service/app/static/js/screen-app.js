/*
 * A tablet or wall screen showing the whole interface ("Pantry Raider mode").
 *
 * Loaded only for a screen's own sign-in (templates add data-screen-id to
 * <html>). Two jobs:
 *
 *   - Leave by itself. After five minutes on the start page with nobody
 *     touching it, the screen is told to close, and its app shows whatever it
 *     showed before. On any other page it stays until someone closes it.
 *   - A finished timer takes over the screen with a Dismiss button, as it
 *     does on the screen's own recipe and timer pages, because the app that
 *     hosts this page hides its own timer bar while a page is open.
 *
 * Plain ES5 on purpose: these tablets run old web views.
 */
(function () {
  'use strict';
  var html = document.documentElement;
  var SCREEN = html.getAttribute('data-screen-id') || '';
  if (!SCREEN) return;
  var IDLE_MS = 5 * 60 * 1000;

  function send(method, url, payload, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.timeout = 40000;
    xhr.setRequestHeader('Accept', 'application/json');
    if (payload) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      }
      if (done) done(data);
    };
    try { xhr.send(payload ? JSON.stringify(payload) : null); } catch (e) { if (done) done(null); }
  }

  var path = window.location.pathname.replace(/\/+$/, '');
  var onStart = /\/ui\/start$/.test(path) || /\/ui$/.test(path);

  // --- the way out -------------------------------------------------------------
  // The page draws its own Exit control, so the app that hosts it needs no
  // close button of its own and no strip of the screen has to be kept clear
  // for one. On the start page it takes the top right corner; on every other
  // page it sits at the right end of the bar across the top. Settings and
  // Sign out mean nothing on a wall tablet, so their controls step aside.
  var chrome = document.createElement('style');
  chrome.textContent =
    '.sa-exit{position:fixed;z-index:2045;right:calc(8px + var(--kiosk-inset-right,0px));' +
    'top:calc(4px + var(--kiosk-inset-top,0px));min-width:64px;height:48px;padding:0 14px;border-radius:12px;' +
    'border:1px solid var(--bs-border-color,#3a3f47);background:rgba(20,22,26,.72);' +
    'background:color-mix(in srgb,var(--bs-body-bg,#14161a) 72%,transparent);' +
    'color:var(--bs-body-color,#fff);font:600 17px/46px inherit;font-family:inherit;text-align:center;cursor:pointer}' +
    '.sa-exit:active{background:var(--bs-primary,#F2006E);color:#fff}' +
    '.sa-exit.start{top:calc(8px + var(--kiosk-inset-top,0px));height:56px;line-height:54px}' +
    '.start-exit,.start-settings{display:none!important}' +
    // A tablet cooks one recipe of its own; courses stay with the kitchen.
    '.pr-tablet-hide{display:none!important}' +
    'nav.navbar a[href="ui/logout"],nav.navbar a[href="setup"],.float-nav a[href="setup"]{display:none!important}' +
    'nav.navbar>.container-fluid,nav.navbar>.container{padding-right:84px!important}';
  document.head.appendChild(chrome);
  function addExit() {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'sa-exit' + (onStart ? ' start' : '');
    b.setAttribute('aria-label', 'Exit Pantry Raider');
    b.textContent = 'Exit';
    b.addEventListener('click', function () {
      b.textContent = '\u2026';
      send('POST', 'events/close', { screen: SCREEN }, function () {
        setTimeout(function () { b.textContent = 'Exit'; }, 3000);
      });
    });
    document.body.appendChild(b);
  }
  if (document.body) addExit();
  else document.addEventListener('DOMContentLoaded', addExit);

  // --- leave after five quiet minutes on the start page -----------------------
  var idleTimer = null;
  function leave() {
    send('POST', 'events/close', { screen: SCREEN }, null);
    // Asked once; if the app does not take the page away, try again later.
    arm();
  }
  function arm() {
    if (idleTimer) clearTimeout(idleTimer);
    if (onStart) idleTimer = setTimeout(leave, IDLE_MS);
  }
  if (onStart) {
    var wake = ['touchstart', 'mousedown', 'keydown', 'wheel'];
    for (var i = 0; i < wake.length; i++) {
      document.addEventListener(wake[i], arm, true);
    }
    arm();
  }

  // --- a finished timer takes over the screen ---------------------------------
  var style = document.createElement('style');
  style.textContent =
    '.sa-ring{position:fixed;top:0;left:0;right:0;bottom:0;z-index:3000;background:#b71c1c;color:#fff;' +
    'display:-webkit-flex;display:flex;-webkit-align-items:center;align-items:center;' +
    '-webkit-justify-content:center;justify-content:center;text-align:center;' +
    'font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif}' +
    '.sa-ring-k{font-size:30px;text-transform:uppercase;letter-spacing:2px}' +
    '.sa-ring-n{font-size:52px;font-weight:700;line-height:1.15;margin:12px 16px 32px 16px}' +
    '.sa-ring-note{font-size:30px;line-height:1.3;margin:-16px 24px 32px 24px;opacity:.95}' +
    '.sa-ring button{height:96px;min-width:240px;margin:0 8px;border:0;border-radius:12px;background:#fff;' +
    'color:#b71c1c;font-size:34px;font-weight:600}' +
    '.sa-ring button.sa-more{background:transparent;color:#fff;border:3px solid #fff}';
  document.head.appendChild(style);

  // What the takeover says: "Bake crust is done." and, for a timer a recipe
  // step started, what comes next.
  function ringText(rows) {
    var names = [], i;
    for (i = 0; i < rows.length; i++) names.push(rows[i].label);
    var last = names.pop();
    var who = names.length ? names.join(', ') + ' and ' + last : last;
    return who + (rows.length > 1 ? ' are done.' : ' is done.');
  }

  var box = null;
  var ringing = [];
  function showRinging(rows) {
    ringing = rows;
    if (!rows.length) {
      if (box && box.parentNode) box.parentNode.removeChild(box);
      box = null;
      return;
    }
    if (!box) {
      box = document.createElement('div');
      box.className = 'sa-ring';
      var inner = document.createElement('div');
      var k = document.createElement('div'); k.className = 'sa-ring-k'; k.textContent = 'Time is up';
      var n = document.createElement('div'); n.className = 'sa-ring-n';
      var note = document.createElement('div'); note.className = 'sa-ring-note';
      var b = document.createElement('button'); b.textContent = 'Dismiss';
      b.addEventListener('click', function () {
        send('POST', 'timers/dismiss', {}, function () { showRinging([]); });
      });
      // +1 min: the same timer again for one more minute.
      var more = document.createElement('button'); more.className = 'sa-more';
      more.textContent = '+1 min';
      more.addEventListener('click', function () {
        var rows = ringing.slice(0);
        showRinging([]);
        for (var i = 0; i < rows.length; i++) {
          (function (t) {
            send('POST', 'timers/' + t.id + '/dismiss', {}, function () {
              send('POST', 'timers', { label: t.label, seconds: 60, origin: t.origin || SCREEN,
                note: t.note || '' }, null);
            });
          })(rows[i]);
        }
      });
      inner.appendChild(k); inner.appendChild(n); inner.appendChild(note);
      inner.appendChild(b); inner.appendChild(more);
      box.appendChild(inner);
      document.body.appendChild(box);
    }
    box.querySelector('.sa-ring-n').textContent = ringText(rows);
    var notes = [];
    for (var j = 0; j < rows.length; j++) if (rows[j].note) notes.push(rows[j].note);
    var noteBox = box.querySelector('.sa-ring-note');
    noteBox.textContent = notes.join(' ');
    noteBox.style.display = notes.length ? '' : 'none';
  }
  function take(timers) {
    var rows = [];
    for (var i = 0; i < (timers || []).length; i++) {
      if (timers[i].expired) rows.push(timers[i]);
    }
    showRinging(rows);
  }

  var cursor = 999999999;
  var delay = 500;
  var gone = false;
  if (window.addEventListener) {
    window.addEventListener('pagehide', function () { gone = true; });
    window.addEventListener('pageshow', function (ev) { if (ev.persisted) { gone = false; watch(); } });
  }
  function watch() {
    if (gone) return;
    // The first call only reads where the list is now, so it is not held.
    var wait = cursor === 999999999 ? 0 : 25;
    send('GET', 'timers/events?wait=' + wait + '&since=' + cursor, null, function (d) {
      if (d) {
        delay = 500;
        if (typeof d.last_id === 'number') cursor = d.last_id;
        take(d.timers);
      } else {
        delay = Math.min(delay * 2, 15000);
      }
      setTimeout(watch, delay);
    });
  }
  if (document.body) watch();
  else document.addEventListener('DOMContentLoaded', watch);
})();
