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
  function bodyClass(name, on) {
    var parts = (' ' + body.className + ' ').replace(' ' + name + ' ', ' ');
    body.className = (parts + (on ? ' ' + name : '')).replace(/^\s+|\s+$/g, '').replace(/\s+/g, ' ');
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

  // A timers page that was opened for a timer puts itself away when the last
  // one is done or dismissed: ten quiet seconds with an empty list, and the
  // screen is told to close. A page opened with nothing running stays, and a
  // timer started during those seconds cancels the close.
  var hadTimer = false;
  var closeTimer = null;
  function watchForEmpty() {
    if (PAGE !== 'timers' || !SCREEN) return;
    if (timers.length) {
      hadTimer = true;
      if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
    } else if (hadTimer && !closeTimer) {
      closeTimer = setTimeout(function () {
        closeTimer = null;
        hadTimer = false;
        request('POST', 'events/close', { screen: SCREEN }, 10000, null);
      }, 10000);
    }
  }

  function takeTimers(list) {
    timers = list || [];
    if (cook) cook.feedTimers(timers);
    watchForEmpty();
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
        bodyClass('has-strip', timers.length > 0);
      }
      // "Bake crust is done." and, for a recipe step's timer, what is next.
      var names = [], notes = [];
      for (i = 0; i < ringing.length; i++) {
        names.push(ringing[i].label);
        if (ringing[i].note) notes.push(ringing[i].note);
      }
      var last = names.pop();
      var said = names.length ? names.join(', ') + ' and ' + last + ' are done.'
        : (last ? last + ' is done.' : '');
      var box = $('sc-ring-names');
      if (box) box.textContent = said;
      var noteBox = $('sc-ring-note');
      if (noteBox) { noteBox.textContent = notes.join(' '); show(noteBox, notes.length > 0); }
      ringingNow = ringing;
      show($('sc-ring'), ringing.length > 0);
    }
    paintTimerButtons();
  }
  var ringingNow = [];
  function tickTimers() {
    var changed = false, i, t, nodes, j;
    for (i = 0; i < timers.length; i++) {
      t = timers[i];
      if (isRinging(t)) { if (shownKey.indexOf(t.id + 'r') < 0) changed = true; continue; }
      nodes = document.querySelectorAll('[data-left="' + t.id + '"]');
      for (j = 0; j < nodes.length; j++) nodes[j].textContent = clockLeft(remaining(t));
    }
    if (changed) renderTimers();
    else paintTimerButtons();
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

  var backBtn = $('sc-step-back');
  if (backBtn) backBtn.addEventListener('click', function () { moveStep('back'); });
  var nextBtn = $('sc-step-next');
  if (nextBtn) nextBtn.addEventListener('click', function () { moveStep('next'); });
  // On a narrow screen the ingredients wait behind a button, after the steps.
  var ingBtn = $('sc-show-ingredients');
  if (ingBtn) ingBtn.addEventListener('click', function () {
    var cols = document.querySelector('.sc-cols');
    if (!cols) return;
    var open = cols.className.indexOf('sc-ingredients-open') < 0;
    cols.className = open ? cols.className + ' sc-ingredients-open'
      : cols.className.replace(/\s*sc-ingredients-open/g, '');
    ingBtn.textContent = open ? 'Hide ingredients' : 'Show ingredients';
    var box = $('sc-ingredients-col');
    if (open && box && box.scrollIntoView) box.scrollIntoView(true);
  });
  var ringBtn = $('sc-ring-dismiss');
  if (ringBtn) ringBtn.addEventListener('click', function () { act('POST', 'timers/dismiss', {}); });
  // +1 min: the timer that rang, once more for a minute.
  var moreBtn = $('sc-ring-more');
  if (moreBtn) moreBtn.addEventListener('click', function () {
    var rows = ringingNow.slice(0);
    for (var r = 0; r < rows.length; r++) {
      (function (t) {
        request('POST', 'timers/' + t.id + '/dismiss', {}, 10000, function () {
          act('POST', 'timers', { label: t.label, seconds: 60, origin: t.origin || SCREEN, note: t.note || '' });
        });
      })(rows[r]);
    }
  });
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

  // --- timers the recipe suggests ---------------------------------------------
  // Each suggested timer has a button in the row above the recipe and one
  // under its step. A button shows whether its timer is counting down (and
  // how long is left), so a second tap never starts a second one.
  var timerButtons = [];
  var finishedKeys = {};
  function suggestionKey(s) { return String(s.label).toLowerCase() + '|' + Math.round(s.seconds); }
  function timerFor(s) {
    // The running (or ringing) timer this suggestion started: same name and
    // length, as the server matches it.
    var want = suggestionKey(s), i, t, ringing = null;
    for (i = 0; i < timers.length; i++) {
      t = timers[i];
      if (String(t.label).toLowerCase() + '|' + Math.round(t.total_seconds) !== want) continue;
      if (!isRinging(t)) return t;
      ringing = t;
    }
    return ringing;
  }
  function paintTimerButton(entry) {
    var s = entry.s, b = entry.el, t = timerFor(s), key = suggestionKey(s);
    var state = 'idle', main = '\u25B6 Start ' + spoken(s.seconds) + ' \u00B7 ' + s.label, sub = '';
    if (t && isRinging(t)) {
      state = 'ringing'; main = s.label + ' \u00B7 Done'; sub = 'Time is up. Tap to stop the alarm';
      finishedKeys[key] = true;
    } else if (t) {
      // Once started, the button is the countdown.
      state = 'running'; main = s.label + ' \u00B7 ' + clockLeft(remaining(t)) + ' left';
      sub = 'Running';
    } else if (finishedKeys[key]) {
      state = 'done'; sub = 'Done. Tap to start it again';
    } else if (entry.quick) {
      sub = 'Step ' + (s.step_index + 1);
    }
    var cls = 'sc-btn sc-tbtn sc-tbtn-' + state;
    if (b.className !== cls) b.className = cls;
    if (entry.main.textContent !== main) entry.main.textContent = main;
    if (entry.sub.textContent !== sub) entry.sub.textContent = sub;
    show(entry.sub, !!sub);
    b.setAttribute('aria-label', state === 'idle' || state === 'done'
      ? 'Start a ' + spoken(s.seconds) + ' timer: ' + s.label : main);
  }
  function paintTimerButtons() {
    for (var i = 0; i < timerButtons.length; i++) paintTimerButton(timerButtons[i]);
  }
  function startSuggested(s) {
    var t = timerFor(s);
    if (t && isRinging(t)) { act('POST', 'timers/' + t.id + '/dismiss'); return; }
    if (t) { toast({ message: s.label + ' is already running, ' + clockLeft(remaining(t)) + ' left.' }); return; }
    request('POST', 'api/screens/recipe/timer',
      { screen: SCREEN, step: s.step_index + 1, label: s.label, origin: SCREEN }, 10000,
      function (d) {
        if (!d) { toast({ level: 'error', message: 'That timer did not start. Try again.' }); return; }
        if (d.result !== 'started' && d.result !== 'running' && d.speech) toast({ message: d.speech });
        delete finishedKeys[suggestionKey(s)];
        loadTimers();
      });
  }
  function timerButton(s, quick) {
    var b = el('button', 'sc-btn sc-tbtn');
    var entry = { s: s, el: b, quick: quick, main: el('span', 'sc-tbtn-main'), sub: el('span', 'sc-tbtn-sub') };
    b.appendChild(entry.main);
    b.appendChild(entry.sub);
    b.addEventListener('click', function (ev) { ev.stopPropagation(); startSuggested(s); });
    timerButtons.push(entry);
    paintTimerButton(entry);
    return b;
  }

  var recipeKey = null;
  var stepOn = -1;
  var stepCount = 0;
  // A step marked when the page opens is shown where it is; one that moves
  // while the page is up (a tap, or voice) is scrolled to.
  var stepSeen = false;
  // Mark the step the cook is on (n from 1, or null); the ones before it dim.
  function applyStep(n, scroll) {
    var ol = $('sc-steps');
    var was = stepOn;
    stepOn = (typeof n === 'number' && n >= 1) ? n - 1 : -1;
    if (!ol) return;
    var all = ol.children;
    for (var i = 0; i < all.length; i++) {
      all[i].className = stepOn < 0 ? '' : (i < stepOn ? 'done' : (i === stepOn ? 'on' : ''));
    }
    var where = $('sc-stepnav-where');
    if (where) {
      where.textContent = stepOn < 0 ? (stepCount ? stepCount + ' steps' : '')
        : 'Step ' + (stepOn + 1) + ' of ' + stepCount;
    }
    var back = $('sc-step-back');
    if (back) back.disabled = stepOn <= 0;
    var next = $('sc-step-next');
    if (next) {
      next.textContent = stepOn < 0 ? 'Start at step 1 \u203A' : 'Next \u203A';
      next.disabled = stepOn >= stepCount - 1 && stepOn >= 0;
    }
    var follow = scroll && stepSeen;
    stepSeen = true;
    if (follow && stepOn >= 0 && stepOn !== was && all[stepOn] && all[stepOn].scrollIntoView) {
      try { all[stepOn].scrollIntoView({ block: 'center', behavior: 'smooth' }); }
      catch (e) { all[stepOn].scrollIntoView(true); }
    }
  }
  function moveStep(which) {
    request('POST', 'api/screens/recipe/step', { screen: SCREEN, step: String(which) }, 10000,
      function (d) {
        if (!d) { toast({ level: 'error', message: 'Could not reach Pantry Raider. Try again.' }); return; }
        if (d.ok === false && d.speech) { toast({ message: d.speech }); return; }
        applyStep(d.step, true);
      });
  }
  function renderRecipe(recipe, suggestions) {
    var key = recipe ? JSON.stringify(recipe) : '';
    if (key === recipeKey) return;
    recipeKey = key;
    var homeCard = $('sc-home-recipe');
    if (homeCard) {
      show(homeCard, !!recipe);
      if (recipe) $('sc-home-recipe-title').textContent = recipe.title || 'Recipe';
      show($('sc-home-empty'), !recipe && !timers.length);
    }
    if (PAGE !== 'recipe') return;
    show($('sc-recipe'), !!recipe);
    show($('sc-recipe-empty'), !recipe);
    timerButtons = [];
    show($('sc-stepnav'), !!recipe);
    bodyClass('has-stepnav', !!recipe);
    if (!recipe) return;
    stepOn = -1;
    stepSeen = false;
    stepCount = (recipe.steps || []).length;
    $('sc-recipe-title').textContent = recipe.title || 'Recipe';
    var servings = recipe.scaled_servings || recipe.servings;
    $('sc-recipe-meta').textContent = servings ? 'Serves ' + amount(servings) : '';

    var quick = $('sc-quick-timers'), q;
    clear(quick);
    suggestions = suggestions || [];
    if (suggestions.length) {
      quick.appendChild(el('div', 'sc-quick-label', suggestions.length === 1
        ? 'Timer in this recipe' : 'Timers in this recipe'));
      var row = el('div', 'sc-tbtn-row');
      for (q = 0; q < suggestions.length; q++) row.appendChild(timerButton(suggestions[q], true));
      quick.appendChild(row);
    }
    show(quick, suggestions.length > 0);

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
        var wrap = null;
        for (var k = 0; k < suggestions.length; k++) {
          if (suggestions[k].step_index !== idx) continue;
          if (!wrap) wrap = el('div', 'sc-step-timers');
          wrap.appendChild(timerButton(suggestions[k], false));
        }
        if (wrap) item.appendChild(wrap);
        // Tap a step to make it the one you are on (tap it again to clear).
        // The server keeps it, so voice ("next step") moves the same marker.
        item.addEventListener('click', function () {
          moveStep(stepOn === idx ? 'none' : idx + 1);
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
    // The recipe open on this screen (its own, or the kitchen's current one).
    var url = 'api/screens/recipe?full=1' + (SCREEN ? '&screen=' + encodeURIComponent(SCREEN) : '');
    request('GET', url, null, 10000, function (d, status) {
      if (d) {
        if (then) then();
        renderRecipe(d.recipe || null, d.suggestions || []);
        if (PAGE === 'recipe') applyStep(d.step, true);
        var stepLine = $('sc-home-recipe-step');
        if (stepLine) {
          stepLine.textContent = typeof d.step === 'number'
            ? 'Step ' + d.step + ' of ' + d.step_count : 'Not started yet';
        }
        return;
      }
      if (status !== 403 && status !== 404) { if (then) then(); return; }
      // This screen is not known (any more): show the kitchen's current recipe.
      request('GET', 'current-recipe', null, 10000, function (c) {
        if (then) then();
        if (!c) return;
        if (!c.recipe || PAGE !== 'recipe') { renderRecipe(c.recipe || null, []); return; }
        request('GET', 'current-recipe/timer-suggestions', null, 10000, function (s) {
          renderRecipe(c.recipe, (s && s.suggestions) || []);
        });
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
    if (here === p.replace(/\/$/, '')) {
      // Already here ("let's cook" something else while cook mode is up).
      if (cook) cook.refresh(); else loadRecipe();
      loadTimers();
      return;
    }
    window.location.assign(p);
  }
  function handle(ev) {
    if (!ev) return;
    if (ev.type === 'navigate' || ev.type === 'close') go(ev);
    else if (ev.type === 'notification' || ev.type === 'confirm') toast(ev);
    else if (ev.type === 'timers') loadTimers();
    else if (ev.type === 'step') { if (cook) cook.refresh(); else loadRecipe(); }
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
  // Cook mode is its own component (static/js/cook-mode.js); this page
  // feeds it the timer list and the events that move its step.
  var cook = null;
  if (PAGE === 'cook' && window.PRCook) {
    cook = window.PRCook.mount($('sc-cook'), {
      screen: SCREEN,
      exit: function () {
        if (!SCREEN) { window.location.assign('screen/recipe'); return; }
        // The app hosting this page closes it; a plain browser is sent home.
        request('POST', 'events/close', { screen: SCREEN }, 10000, null);
      }
    });
  }
  loadTimers();
  pollTimers();
  if (PAGE !== 'timers' && PAGE !== 'cook') {
    (function recipeLoop() { loadRecipe(function () { setTimeout(recipeLoop, 10000); }); })();
  }
  pollEvents();
})();
