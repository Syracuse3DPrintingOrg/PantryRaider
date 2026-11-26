/*
 * The pages a tablet or wall screen shows: timers and the recipe being cooked
 * (templates/screen.html).
 *
 * Written to run on old tablet browsers (an Android 7 system web view): plain
 * ES5, XMLHttpRequest, no arrow functions, no template strings, no fetch.
 *
 * Three loops, each a held request so the screen reacts at once:
 *   - timers/events   the timer list and what changed in it
 *   - api/screens/poll  events sent to this one screen (a page change, a toast)
 *   - current-recipe  re-read every few seconds on the pages that show it
 */
(function () {
  'use strict';

  var body = document.body;
  var PAGE = body.getAttribute('data-page') || 'home';
  var SCREEN = body.getAttribute('data-screen') || '';
  var WAIT_S = 25;

  function $(id) { return document.getElementById(id); }
  function show(el, on) { if (el) { if (on) el.removeAttribute('hidden'); else el.setAttribute('hidden', ''); } }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.appendChild(document.createTextNode(String(text)));
    return e;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  // --- requests ---------------------------------------------------------------
  var failures = 0;
  function note(ok) {
    failures = ok ? 0 : failures + 1;
    show($('sc-offline'), failures >= 3);
  }
  // done(data, status); data is null when the request failed or was not JSON.
  function request(method, url, payload, timeoutMs, done) {
    var xhr = new XMLHttpRequest();
    var finished = false;
    function finish(data, status) {
      if (finished) return;
      finished = true;
      if (done) done(data, status);
    }
    xhr.open(method, url, true);
    xhr.timeout = timeoutMs || 15000;
    xhr.setRequestHeader('Accept', 'application/json');
    if (payload !== null && payload !== undefined) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      // A sign-in that ran out answers with the sign-in page instead of data.
      var data = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      }
      finish(data, xhr.status);
    };
    xhr.onerror = function () { finish(null, 0); };
    xhr.ontimeout = function () { finish(null, 0); };
    try {
      xhr.send(payload !== null && payload !== undefined ? JSON.stringify(payload) : null);
    } catch (e) { finish(null, 0); }
    return xhr;
  }

  // --- clock ------------------------------------------------------------------
  var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
    'September', 'October', 'November', 'December'];
  function clockText(d) {
    var h = d.getHours(), m = d.getMinutes();
    var h12 = h % 12; if (h12 === 0) h12 = 12;
    return h12 + ':' + (m < 10 ? '0' : '') + m;
  }
  function tickClock() {
    var d = new Date();
    var small = $('sc-clock'), big = $('sc-bigclock'), date = $('sc-date');
    if (small) small.textContent = clockText(d);
    if (big) big.textContent = clockText(d);
    if (date) date.textContent = DAYS[d.getDay()] + ', ' + MONTHS[d.getMonth()] + ' ' + d.getDate();
  }

  // --- timers -----------------------------------------------------------------
  var timers = [];
  // Server clock minus this tablet's clock, in seconds. Old tablets drift, so
  // countdowns are worked out from the server's idea of now.
  var skew = 0;
  var timerCursor = 999999999;

  function serverNow() { return Date.now() / 1000 + skew; }
  function remaining(t) { return Math.max(0, t.deadline_epoch - serverNow()); }
  function isRinging(t) { return remaining(t) <= 0; }
  function clockLeft(sec) {
    sec = Math.ceil(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    var mm = (h && m < 10 ? '0' : '') + m, ss = (s < 10 ? '0' : '') + s;
    return (h ? h + ':' : '') + mm + ':' + ss;
  }

  function takeTimers(list) {
    timers = list || [];
    for (var i = 0; i < timers.length; i++) {
      var t = timers[i];
      if (t.running && typeof t.remaining_seconds === 'number') {
        skew = (t.deadline_epoch - t.remaining_seconds) - Date.now() / 1000;
        break;
      }
    }
    renderTimers();
  }

  function act(method, url, payload) {
    request(method, url, payload === undefined ? null : payload, 10000, function () { loadTimers(); });
  }
  function button(text, cls, handler) {
    var b = el('button', 'sc-btn' + (cls ? ' ' + cls : ''), text);
    b.addEventListener('click', handler);
    return b;
  }

  function timerRow(t) {
    var ringing = isRinging(t);
    var row = el('div', 'sc-timer' + (ringing ? ' ringing' : ''));
    row.setAttribute('data-id', t.id);
    var info = el('div', 'sc-timer-info');
    info.appendChild(el('div', 'sc-timer-label', t.label));
    var left = el('div', 'sc-timer-left', ringing ? 'Done' : clockLeft(remaining(t)));
    left.setAttribute('data-left', t.id);
    info.appendChild(left);
    row.appendChild(info);
    var acts = el('div', 'sc-timer-acts');
    if (ringing) {
      acts.appendChild(button('Dismiss', '', function () { act('POST', 'timers/' + t.id + '/dismiss'); }));
    } else {
      acts.appendChild(button('+1 min', '', function () { act('POST', 'timers/' + t.id + '/extend', { seconds: 60 }); }));
      acts.appendChild(button('Cancel', 'sc-btn-danger', function () { act('DELETE', 'timers/' + t.id); }));
    }
    row.appendChild(acts);
    return row;
  }

  var shownKey = '';
  function renderTimers() {
    // Rebuild only when the set of timers or their state changes; the
    // countdown text is updated in place every second.
    var key = '', ringing = [], i, t;
    for (i = 0; i < timers.length; i++) {
      t = timers[i];
      key += t.id + (isRinging(t) ? 'r' : 'c') + t.label + '|';
      if (isRinging(t)) ringing.push(t);
    }
    if (key !== shownKey) {
      shownKey = key;
      var list = $('sc-timers') || $('sc-home-timers');
      if (list) {
        clear(list);
        for (i = 0; i < timers.length; i++) list.appendChild(timerRow(timers[i]));
      }
      show($('sc-timers-empty'), timers.length === 0);
      var strip = $('sc-strip');
      if (strip && PAGE === 'recipe') {
        clear(strip);
        for (i = 0; i < timers.length; i++) {
          t = timers[i];
          var chip = el('a', 'sc-chip' + (isRinging(t) ? ' ringing' : ''), t.label);
          chip.href = 'screen/timers';
          var b = el('b', '', isRinging(t) ? 'Done' : clockLeft(remaining(t)));
          b.setAttribute('data-left', t.id);
          chip.appendChild(b);
          strip.appendChild(chip);
        }
        show(strip, timers.length > 0);
        body.className = timers.length > 0 ? 'has-strip' : '';
      }
      var names = [];
      for (i = 0; i < ringing.length; i++) names.push(ringing[i].label);
      var box = $('sc-ring-names');
      if (box) box.textContent = names.join(', ');
      show($('sc-ring'), ringing.length > 0);
    }
  }
  function tickTimers() {
    var changed = false, i, t, nodes, j;
    for (i = 0; i < timers.length; i++) {
      t = timers[i];
      if (isRinging(t)) { if (shownKey.indexOf(t.id + 'r') < 0) changed = true; continue; }
      nodes = document.querySelectorAll('[data-left="' + t.id + '"]');
      for (j = 0; j < nodes.length; j++) nodes[j].textContent = clockLeft(remaining(t));
    }
    if (changed) renderTimers();
  }

  function loadTimers() {
    request('GET', 'timers', null, 10000, function (d) {
      if (d && d.timers) takeTimers(d.timers);
    });
  }
  var timerDelay = 200;
  function pollTimers() {
    // The first call only reads where the list is now, so it is not held.
    var wait = timerCursor === 999999999 ? 0 : WAIT_S;
    request('GET', 'timers/events?wait=' + wait + '&since=' + timerCursor, null,
      (WAIT_S + 10) * 1000, function (d) {
        note(!!d);
        if (d) {
          timerDelay = 200;
          if (typeof d.last_id === 'number') timerCursor = d.last_id;
          if (d.timers) takeTimers(d.timers);
        } else {
          timerDelay = Math.min(timerDelay * 2, 15000);
        }
        setTimeout(pollTimers, timerDelay);
      });
  }

  var ringBtn = $('sc-ring-dismiss');
  if (ringBtn) ringBtn.addEventListener('click', function () { act('POST', 'timers/dismiss', {}); });
  var starts = document.querySelectorAll('[data-start]');
  for (var si = 0; si < starts.length; si++) {
    (function (b) {
      b.addEventListener('click', function () {
        var secs = parseInt(b.getAttribute('data-start'), 10);
        act('POST', 'timers', { label: b.textContent + ' timer', seconds: secs, origin: SCREEN });
      });
    })(starts[si]);
  }

  // --- recipe -----------------------------------------------------------------
  var FRACTIONS = [[0.125, '⅛'], [0.25, '¼'], [0.333, '⅓'], [0.375, '⅜'],
    [0.5, '½'], [0.625, '⅝'], [0.667, '⅔'], [0.75, '¾'], [0.875, '⅞']];
  function amount(q) {
    if (q === null || q === undefined || isNaN(q)) return '';
    var whole = Math.floor(q), part = q - whole, i;
    if (part < 0.02) return String(whole);
    if (part > 0.98) return String(whole + 1);
    for (i = 0; i < FRACTIONS.length; i++) {
      if (Math.abs(part - FRACTIONS[i][0]) < 0.02) return (whole ? whole + ' ' : '') + FRACTIONS[i][1];
    }
    return String(Math.round(q * 100) / 100);
  }

  function spoken(sec) {
    var h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    if (h && m) return h + ' hr ' + m + ' min';
    if (h) return h + ' hr';
    return m ? m + ' min' : Math.round(sec) + ' sec';
  }

  var recipeKey = null;
  var stepOn = -1;
  function renderRecipe(recipe, suggestions) {
    var key = recipe ? JSON.stringify(recipe) : '';
    if (key === recipeKey) return;
    recipeKey = key;
    var homeCard = $('sc-home-recipe');
    if (homeCard) {
      show(homeCard, !!recipe);
      if (recipe) $('sc-home-recipe-title').textContent = recipe.title || 'Recipe';
    }
    if (PAGE !== 'recipe') return;
    show($('sc-recipe'), !!recipe);
    show($('sc-recipe-empty'), !recipe);
    if (!recipe) return;
    stepOn = -1;
    $('sc-recipe-title').textContent = recipe.title || 'Recipe';
    var servings = recipe.scaled_servings || recipe.servings;
    $('sc-recipe-meta').textContent = servings ? 'Serves ' + amount(servings) : '';

    var ul = $('sc-ingredients'), i, section = null;
    clear(ul);
    var ings = recipe.ingredients || [];
    for (i = 0; i < ings.length; i++) {
      var ing = ings[i];
      if (ing.section && ing.section !== section) {
        section = ing.section;
        ul.appendChild(el('li', 'sc-section', section));
      }
      var li = el('li');
      var qty = amount(ing.scaled_quantity !== undefined && ing.scaled_quantity !== null
        ? ing.scaled_quantity : ing.quantity);
      var lead = (qty + ' ' + (ing.unit || '')).replace(/^\s+|\s+$/g, '');
      if (lead) { li.appendChild(el('span', 'sc-qty', lead)); li.appendChild(document.createTextNode(' ')); }
      li.appendChild(document.createTextNode(ing.name || ''));
      li.addEventListener('click', function () {
        this.className = this.className === 'done' ? '' : 'done';
      });
      ul.appendChild(li);
    }

    var ol = $('sc-steps');
    clear(ol);
    var steps = recipe.steps || [];
    for (i = 0; i < steps.length; i++) {
      (function (idx) {
        var item = el('li', '', steps[idx]);
        var mine = [];
        for (var k = 0; k < (suggestions || []).length; k++) {
          if (suggestions[k].step_index === idx) mine.push(suggestions[k]);
        }
        if (mine.length) {
          var wrap = el('div', 'sc-step-timers');
          for (var m = 0; m < mine.length; m++) {
            (function (s) {
              wrap.appendChild(button(s.label + ' \u00B7 ' + spoken(s.seconds), '', function (ev) {
                ev.stopPropagation();
                request('POST', 'current-recipe/timers/start',
                  { step_index: s.step_index, label: s.label, seconds: s.seconds }, 10000,
                  function () { loadTimers(); });
              }));
            })(mine[m]);
          }
          item.appendChild(wrap);
        }
        // Tap a step to mark where you are; the ones before it dim.
        item.addEventListener('click', function () {
          stepOn = stepOn === idx ? -1 : idx;
          var all = ol.children;
          for (var n = 0; n < all.length; n++) {
            all[n].className = stepOn < 0 ? '' : (n < stepOn ? 'done' : (n === stepOn ? 'on' : ''));
          }
        });
        ol.appendChild(item);
      })(i);
    }
    var notes = $('sc-notes');
    notes.textContent = recipe.notes || '';
    show(notes, !!recipe.notes);
    $('sc-main').scrollTop = 0;
  }

  function loadRecipe(then) {
    request('GET', 'current-recipe', null, 10000, function (d) {
      if (then) then();
      if (!d) return;
      if (!d.recipe || PAGE !== 'recipe') { renderRecipe(d.recipe || null, []); return; }
      request('GET', 'current-recipe/timer-suggestions', null, 10000, function (s) {
        renderRecipe(d.recipe, (s && s.suggestions) || []);
      });
    });
  }

  // --- events sent to this screen ---------------------------------------------
  function toast(ev) {
    var box = el('div', 'sc-toast ' + (ev.level || 'info'));
    if (ev.title) box.appendChild(el('b', '', ev.title));
    box.appendChild(document.createTextNode(ev.message || ''));
    $('sc-toasts').appendChild(box);
    setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); },
      (ev.timeout > 0 ? ev.timeout : 8) * 1000);
  }
  function go(ev) {
    var p = String(ev.path || '').replace(/^\/+/, '');
    // Same-site pages only, the server already checked.
    if (!p || /^[a-z][a-z0-9+.-]*:/i.test(p) || p.indexOf('//') === 0) return;
    var here = window.location.pathname.replace(/^\/+/, '').replace(/\/$/, '');
    if (here === p.replace(/\/$/, '')) { loadRecipe(); loadTimers(); return; }
    window.location.assign(p);
  }
  function handle(ev) {
    if (!ev) return;
    if (ev.type === 'navigate' || ev.type === 'close') go(ev);
    else if (ev.type === 'notification' || ev.type === 'confirm') toast(ev);
    else if (ev.type === 'timers') loadTimers();
  }
  var eventCursor = 999999999;
  var eventDelay = 200;
  function pollEvents() {
    if (!SCREEN) return;
    var wait = eventCursor === 999999999 ? 0 : WAIT_S;
    request('GET', 'api/screens/poll?screen=' + encodeURIComponent(SCREEN) + '&wait=' + wait +
      '&since=' + eventCursor, null, (WAIT_S + 10) * 1000, function (d, status) {
        if (d) {
          eventDelay = 200;
          var first = eventCursor === 999999999;
          if (typeof d.last_id === 'number') eventCursor = d.last_id;
          if (!first) { for (var i = 0; i < (d.events || []).length; i++) handle(d.events[i]); }
        } else {
          // 403 or 404: this screen is not known here (any more). Ask rarely.
          eventDelay = (status === 403 || status === 404) ? 60000 : Math.min(eventDelay * 2, 15000);
        }
        setTimeout(pollEvents, eventDelay);
      });
  }

  // --- go ---------------------------------------------------------------------
  // Each loop books its next run when the last one is done, so a slow tablet
  // or a slow answer never stacks them.
  (function clockLoop() { tickClock(); setTimeout(clockLoop, 5000); })();
  (function countLoop() { tickTimers(); setTimeout(countLoop, 500); })();
  loadTimers();
  pollTimers();
  if (PAGE !== 'timers') {
    (function recipeLoop() { loadRecipe(function () { setTimeout(recipeLoop, 10000); }); })();
  }
  pollEvents();
})();
