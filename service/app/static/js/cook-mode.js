/*
 * Cook mode: the recipe being cooked, one large step at a time.
 *
 * One component for both places it appears: a tablet's /screen/cook page
 * (templates/screen.html) and the full interface's On the Line page
 * (templates/current-recipe.html, opened with "Start cooking"). Plain ES5
 * and XMLHttpRequest so it runs on old tablet web views and opens fast.
 *
 * The step shown is the screen's current step, kept on the server
 * (POST api/screens/recipe/step). Back, Next, a swipe and voice ("next
 * step") all move that same marker, so this page follows whatever moved it.
 *
 *   PRCook.mount(root, {
 *     screen: 'ks-echo',     // the screen's id; '' for this kitchen's own
 *     exit: function () {},  // what Exit does
 *     ownTimers: true        // poll the timer list here (the full page);
 *   })                       // /screen pages feed it through feedTimers()
 */
(function () {
  'use strict';

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.appendChild(document.createTextNode(String(text)));
    return e;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  // How many lines of text an element shows now (0 while it is hidden).
  function linesOf(node) {
    var cs = window.getComputedStyle ? window.getComputedStyle(node) : null;
    var lh = cs ? parseFloat(cs.lineHeight) : NaN;
    if (!(lh > 0)) lh = (cs ? parseFloat(cs.fontSize) : 40) * 1.3;
    return Math.round(node.offsetHeight / lh);
  }
  function show(node, on) { if (node) { if (on) node.removeAttribute('hidden'); else node.setAttribute('hidden', ''); } }

  function request(method, url, payload, done) {
    var xhr = new XMLHttpRequest();
    var finished = false;
    function finish(data, status) {
      if (finished) return;
      finished = true;
      if (done) done(data, status);
    }
    xhr.open(method, url, true);
    xhr.timeout = 15000;
    xhr.setRequestHeader('Accept', 'application/json');
    if (payload) xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      }
      finish(data, xhr.status);
    };
    xhr.onerror = function () { finish(null, 0); };
    xhr.ontimeout = function () { finish(null, 0); };
    try { xhr.send(payload ? JSON.stringify(payload) : null); } catch (e) { finish(null, 0); }
  }

  function spoken(sec) {
    var h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    if (h && m) return h + ' hr ' + m + ' min';
    if (h) return h + ' hr';
    return m ? m + ' min' : Math.round(sec) + ' sec';
  }
  function clockLeft(sec) {
    sec = Math.max(0, Math.ceil(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ':' : '') + (h && m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  var FRACTIONS = [[0.125, '⅛'], [0.25, '¼'], [0.333, '⅓'], [0.5, '½'],
    [0.667, '⅔'], [0.75, '¾']];
  function amount(q) {
    if (q === null || q === undefined || isNaN(q) || q === '') return '';
    q = Number(q);
    var whole = Math.floor(q), part = q - whole, i;
    if (part < 0.02) return String(whole);
    if (part > 0.98) return String(whole + 1);
    for (i = 0; i < FRACTIONS.length; i++) {
      if (Math.abs(part - FRACTIONS[i][0]) < 0.02) return (whole ? whole + ' ' : '') + FRACTIONS[i][1];
    }
    return String(Math.round(q * 100) / 100);
  }
  function ingredientText(ing) {
    var qty = ing.scaled_quantity !== undefined && ing.scaled_quantity !== null ? ing.scaled_quantity : ing.quantity;
    return ((amount(qty) + ' ' + (ing.unit || '')).replace(/^\s+|\s+$/g, '') + ' ' + (ing.name || '')).replace(/^\s+/, '');
  }
  // Whether a step mentions an ingredient: any longer word of its name.
  function mentions(stepText, ing) {
    var text = ' ' + String(stepText || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ') + ' ';
    var words = String(ing.name || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(' ');
    for (var i = 0; i < words.length; i++) {
      var w = words[i];
      if (w.length < 4) continue;
      if (w.length > 4 && w.charAt(w.length - 1) === 's') w = w.slice(0, -1);
      if (text.indexOf(' ' + w) >= 0) return true;
    }
    return false;
  }

  function mount(root, opts) {
    opts = opts || {};
    var SCREEN = opts.screen || '';
    var q = SCREEN ? '&screen=' + encodeURIComponent(SCREEN) : '';
    var recipe = null, suggestions = [], cur = -1, finishedView = false;
    var recipeKey = '';
    var timers = [], skew = 0, finished = {};
    var buttons = [];
    var moving = false;

    // --- the frame, built once ------------------------------------------------
    clear(root);
    root.className = (root.className ? root.className + ' ' : '') + 'cm';
    var top = el('div', 'cm-top');
    var title = el('div', 'cm-title', '');
    var where = el('div', 'cm-where', '');
    var exitBtn = el('button', 'cm-btn cm-exit', 'Exit');
    top.appendChild(title); top.appendChild(where); top.appendChild(exitBtn);
    var runRow = el('div', 'cm-runrow');
    var body = el('div', 'cm-body');
    var main = el('div', 'cm-main');
    var kicker = el('div', 'cm-kicker', '');
    var dots = el('div', 'cm-dots');
    var text = el('div', 'cm-text', '');
    var stepTimers = el('div', 'cm-timers');
    main.appendChild(kicker); main.appendChild(dots); main.appendChild(text); main.appendChild(stepTimers);
    var side = el('div', 'cm-side');
    var runSide = el('div', 'cm-running');
    var nextUp = el('div', 'cm-nextup');
    var ingSide = el('button', 'cm-btn cm-ing-btn', 'Ingredients');
    side.appendChild(runSide); side.appendChild(nextUp); side.appendChild(ingSide);
    body.appendChild(main); body.appendChild(side);
    var done = el('div', 'cm-done');
    var bar = el('div', 'cm-bar');
    var backBtn = el('button', 'cm-btn cm-back', '‹ Back');
    var nextBtn = el('button', 'cm-btn cm-next', 'Next ›');
    var ingBar = el('button', 'cm-btn cm-ing-bar', 'Ingredients');
    bar.appendChild(backBtn); bar.appendChild(nextBtn); bar.appendChild(ingBar);
    var sheet = el('div', 'cm-sheet');
    var empty = el('div', 'cm-empty', '');
    root.appendChild(top); root.appendChild(runRow); root.appendChild(body);
    root.appendChild(done); root.appendChild(empty); root.appendChild(bar); root.appendChild(sheet);
    show(sheet, false); show(done, false); show(empty, false);

    exitBtn.addEventListener('click', function () { if (opts.exit) opts.exit(); });
    backBtn.addEventListener('click', function () {
      if (finishedView) {
        finishedView = false;
        if (!cookedOk) cookedNote = '';   // a Mark as cooked that failed can be tried again
        paint();
        return;
      }
      move('back');
    });
    nextBtn.addEventListener('click', function () {
      if (recipe && cur >= (recipe.steps || []).length - 1) { finishedView = true; paint(); return; }
      move(cur < 0 ? 'first' : 'next');
    });
    ingSide.addEventListener('click', openSheet);
    ingBar.addEventListener('click', openSheet);

    // A swipe across the step moves it too.
    var sx = null, sy = null;
    body.addEventListener('touchstart', function (ev) {
      if (!ev.touches || ev.touches.length !== 1) return;
      sx = ev.touches[0].clientX; sy = ev.touches[0].clientY;
    });
    body.addEventListener('touchend', function (ev) {
      if (sx === null || !ev.changedTouches || !ev.changedTouches.length) return;
      var dx = ev.changedTouches[0].clientX - sx, dy = ev.changedTouches[0].clientY - sy;
      sx = null;
      if (Math.abs(dx) < 70 || Math.abs(dy) > Math.abs(dx) * 0.6) return;
      if (dx < 0) nextBtn.click(); else backBtn.click();
    });

    // --- the step marker ------------------------------------------------------
    function move(which) {
      if (moving) return;
      moving = true;
      // adopt: cooking the kitchen's recipe here makes it this screen's own,
      // so voice at this screen follows it too.
      request('POST', 'api/screens/recipe/step', { screen: SCREEN, step: String(which), adopt: true }, function (d) {
        moving = false;
        if (d && typeof d.step === 'number') { cur = d.step - 1; finishedView = false; paint(); }
      });
    }

    function load(then) {
      request('GET', 'api/screens/recipe?full=1' + q, null, function (d) {
        if (typeof then === 'function') then();
        if (!d) return;
        if (!d.ok || !d.recipe) {
          // Just marked cooked: the recipe is put away, and the page keeps
          // saying so until Exit.
          if (cookedOk && finishedView) return;
          recipe = null; recipeKey = ''; paint(); return;
        }
        var key = JSON.stringify(d.recipe);
        if (key !== recipeKey) {
          recipeKey = key; recipe = d.recipe; suggestions = d.suggestions || [];
          finishedView = false; cookedNote = ''; cookedOk = false;
        }
        var was = cur;
        cur = typeof d.step === 'number' ? d.step - 1 : -1;
        if (cur < 0 && (recipe.steps || []).length) { move('first'); }
        if (was !== cur || !text.firstChild) paint();
      });
    }

    // --- timer buttons: a start button that becomes its countdown -------------
    function serverNow() { return Date.now() / 1000 + skew; }
    function keyOf(s) { return String(s.label).toLowerCase() + '|' + Math.round(s.seconds); }
    function timerFor(s) {
      var want = keyOf(s), ringing = null;
      for (var i = 0; i < timers.length; i++) {
        var t = timers[i];
        if (String(t.label).toLowerCase() + '|' + Math.round(t.total_seconds) !== want) continue;
        if (t.deadline_epoch - serverNow() > 0) return t;
        ringing = t;
      }
      return ringing;
    }
    function paintButton(b) {
      var s = b.s, t = timerFor(s), state = 'idle';
      var main = '▶ Start ' + spoken(s.seconds) + ' · ' + s.label, sub = '';
      if (t && t.deadline_epoch - serverNow() <= 0) {
        state = 'ringing'; main = s.label + ' · Done'; sub = 'Time is up. Tap to stop the alarm';
        finished[keyOf(s)] = true;
      } else if (t) {
        state = 'running'; main = s.label + ' · ' + clockLeft(t.deadline_epoch - serverNow()) + ' left';
        sub = 'Running';
      } else if (finished[keyOf(s)]) {
        state = 'done'; sub = 'Done. Tap to start it again';
      }
      var cls = 'cm-btn cm-tbtn cm-tbtn-' + state;
      if (b.el.className !== cls) b.el.className = cls;
      if (b.main.textContent !== main) b.main.textContent = main;
      if (b.sub.textContent !== sub) b.sub.textContent = sub;
      show(b.sub, !!sub);
    }
    function start(s) {
      var t = timerFor(s);
      if (t && t.deadline_epoch - serverNow() <= 0) {
        request('POST', 'timers/' + t.id + '/dismiss', {}, function () { loadTimers(); });
        return;
      }
      if (t) return;   // already counting down: never a second one
      request('POST', 'api/screens/recipe/timer',
        { screen: SCREEN, step: s.step_index + 1, label: s.label, origin: SCREEN, adopt: true }, function () {
          delete finished[keyOf(s)];
          loadTimers();
        });
    }
    function timerButton(s) {
      var b = { s: s, el: el('button', 'cm-btn cm-tbtn'), main: el('span', 'cm-tbtn-main'), sub: el('span', 'cm-tbtn-sub') };
      b.el.appendChild(b.main); b.el.appendChild(b.sub);
      b.el.addEventListener('click', function () { start(s); });
      buttons.push(b);
      paintButton(b);
      return b.el;
    }

    function paintRunning() {
      var box, i, t, list = [];
      for (i = 0; i < timers.length; i++) list.push(timers[i]);
      var boxes = [runSide, runRow];
      for (var k = 0; k < boxes.length; k++) {
        box = boxes[k];
        var sig = '';
        for (i = 0; i < list.length; i++) sig += list[i].id + (list[i].deadline_epoch - serverNow() <= 0 ? 'r' : 'c') + '|';
        if (box.getAttribute('data-sig') !== sig) {
          box.setAttribute('data-sig', sig);
          clear(box);
          if (k === 0 && list.length) box.appendChild(el('div', 'cm-label', 'Timers'));
          for (i = 0; i < list.length; i++) {
            t = list[i];
            var row = el('div', 'cm-run');
            row.appendChild(el('span', 'cm-run-name', t.label));
            var left = el('span', 'cm-run-left', '');
            left.setAttribute('data-id', t.id);
            row.appendChild(left);
            box.appendChild(row);
          }
        }
        var lefts = box.querySelectorAll('.cm-run-left');
        for (i = 0; i < lefts.length; i++) {
          for (var j = 0; j < list.length; j++) {
            if (String(list[j].id) !== lefts[i].getAttribute('data-id')) continue;
            var r = list[j].deadline_epoch - serverNow();
            lefts[i].textContent = r <= 0 ? 'Done' : clockLeft(r);
            lefts[i].parentNode.className = 'cm-run' + (r <= 0 ? ' ringing' : '');
          }
        }
      }
      show(runRow, list.length > 0);
    }
    function tick() {
      for (var i = 0; i < buttons.length; i++) paintButton(buttons[i]);
      paintRunning();
    }

    // --- drawing a step --------------------------------------------------------
    function paint() {
      buttons = [];
      if (!recipe) {
        show(body, false); show(done, false); show(bar, false); show(runRow, false);
        empty.textContent = 'No recipe is open. Ask for one, or pick one in Pantry Raider.';
        show(empty, true);
        title.textContent = 'Cook mode';
        where.textContent = '';
        return;
      }
      show(empty, false); show(bar, true);
      var steps = recipe.steps || [], n = steps.length;
      title.textContent = recipe.title || 'Recipe';
      var idx = cur < 0 ? 0 : Math.min(cur, n - 1);
      if (finishedView) {
        show(body, false); show(done, true);
        where.textContent = 'Finished';
        paintDone();
        backBtn.disabled = false;
        nextBtn.disabled = true;
        nextBtn.textContent = 'Next ›';
        return;
      }
      show(done, false); show(body, true);
      where.textContent = n ? 'Step ' + (idx + 1) + ' of ' + n : '';
      kicker.textContent = n ? 'Step ' + (idx + 1) + ' of ' + n : 'No steps written down';
      clear(dots);
      for (var d = 0; d < n && n <= 30; d++) dots.appendChild(el('span', d < idx ? 'past' : (d === idx ? 'on' : '')));
      var stepText = n ? String(steps[idx]) : '';
      text.textContent = stepText;
      text.className = 'cm-text';
      // Past six lines at the usual size, a step gets the smaller type.
      if (stepText && linesOf(text) > 6) text.className = 'cm-text cm-long';
      clear(stepTimers);
      for (var i = 0; i < suggestions.length; i++) {
        if (suggestions[i].step_index === idx) stepTimers.appendChild(timerButton(suggestions[i]));
      }
      show(stepTimers, stepTimers.firstChild !== null);
      clear(nextUp);
      if (idx + 1 < n) {
        nextUp.appendChild(el('div', 'cm-label', 'Next up'));
        nextUp.appendChild(el('div', 'cm-nextup-text', steps[idx + 1]));
      } else if (n) {
        nextUp.appendChild(el('div', 'cm-label', 'Last step'));
      }
      backBtn.disabled = idx <= 0;
      nextBtn.disabled = false;
      nextBtn.textContent = idx >= n - 1 ? 'Finish ›' : 'Next ›';
      if (!sheet.hasAttribute('hidden')) openSheet();
      tick();
    }

    // --- the end: mark it cooked ----------------------------------------------
    var cookedNote = '', cookedOk = false;
    function paintDone() {
      clear(done);
      done.appendChild(el('div', 'cm-done-title', !cookedNote ? 'That was the last step'
        : (cookedOk ? 'Cooked' : 'Not marked cooked')));
      done.appendChild(el('div', 'cm-done-text', cookedNote ||
        'Mark it cooked to use up the matching items in your pantry, or go back to check a step.'));
      var row = el('div', 'cm-done-acts');
      if (!cookedNote) {
        var mark = el('button', 'cm-btn cm-primary', 'Mark as cooked');
        mark.addEventListener('click', function () {
          clear(row);
          row.appendChild(el('div', 'cm-done-text', 'This uses up the matching items in your pantry.'));
          var yes = el('button', 'cm-btn cm-primary', 'Use them up');
          var no = el('button', 'cm-btn', 'Not yet');
          yes.addEventListener('click', function () { yes.disabled = true; cooked(); });
          no.addEventListener('click', paintDone);
          row.appendChild(yes); row.appendChild(no);
        });
        row.appendChild(mark);
      }
      var exit = el('button', 'cm-btn', 'Exit');
      exit.addEventListener('click', function () { if (opts.exit) opts.exit(); });
      row.appendChild(exit);
      done.appendChild(row);
    }
    function cooked() {
      function finish(d) {
        // ok false is an answer too ("No recipe is open."): nothing was used up.
        cookedOk = !!(d && d.ok !== false);
        var n = cookedOk && d.consumed ? d.consumed.length : 0;
        cookedNote = cookedOk ? (n + ' item' + (n === 1 ? '' : 's') + ' used up from your pantry. ' +
          'A "save to leftovers?" item is waiting on the Pending page.')
          : ((d && d.speech) || 'That did not work. Try again from the recipe page.');
        paintDone();
      }
      request('POST', 'api/screens/recipe/cooked', { screen: SCREEN }, function (d, status) {
        // A Bandit marks its own recipe cooked the way its recipe page does.
        if (status === 409) { request('POST', 'current-recipe/cooked', {}, function (c) { finish(c); }); return; }
        finish(d);
      });
    }

    // --- ingredients, led by the ones this step uses --------------------------
    function openSheet() {
      if (!recipe) return;
      clear(sheet);
      var box = el('div', 'cm-sheet-box');
      var head = el('div', 'cm-sheet-head');
      head.appendChild(el('div', 'cm-sheet-title', 'Ingredients'));
      var close = el('button', 'cm-btn', 'Close');
      close.addEventListener('click', function () { show(sheet, false); });
      head.appendChild(close);
      box.appendChild(head);
      var steps = recipe.steps || [], idx = cur < 0 ? 0 : cur;
      var ings = recipe.ingredients || [], here = [], rest = [], i;
      for (i = 0; i < ings.length; i++) (mentions(steps[idx], ings[i]) ? here : rest).push(ings[i]);
      var list = el('div', 'cm-sheet-list');
      if (here.length) {
        list.appendChild(el('div', 'cm-label', 'For this step'));
        for (i = 0; i < here.length; i++) list.appendChild(el('div', 'cm-ing cm-ing-here', ingredientText(here[i])));
        if (rest.length) list.appendChild(el('div', 'cm-label', 'The rest'));
      }
      for (i = 0; i < rest.length; i++) list.appendChild(el('div', 'cm-ing', ingredientText(rest[i])));
      box.appendChild(list);
      sheet.appendChild(box);
      show(sheet, true);
    }
    sheet.addEventListener('click', function (ev) { if (ev.target === sheet) show(sheet, false); });

    // --- timers ------------------------------------------------------------------
    function feedTimers(list) {
      timers = list || [];
      for (var i = 0; i < timers.length; i++) {
        var t = timers[i];
        if (t.running && typeof t.remaining_seconds === 'number') {
          skew = (t.deadline_epoch - t.remaining_seconds) - Date.now() / 1000;
          break;
        }
      }
      tick();
    }
    function loadTimers(then) {
      request('GET', 'timers', null, function (d) {
        if (typeof then === 'function') then();
        if (d && d.timers) feedTimers(d.timers);
      });
    }

    (function tickLoop() { tick(); setTimeout(tickLoop, 500); })();
    // Follow the marker when something else moves it: an event says so on a
    // /screen page; the full page reads it every few seconds. Each read books
    // the next one once it has answered, so a slow answer never stacks them.
    (function recipeLoop() {
      load(function () { setTimeout(recipeLoop, opts.ownTimers ? 3000 : 10000); });
    })();
    if (opts.ownTimers) {
      (function timerLoop() { loadTimers(function () { setTimeout(timerLoop, 3000); }); })();
    } else {
      loadTimers();
    }
    return { feedTimers: feedTimers, refresh: function () { load(); } };
  }

  window.PRCook = { mount: mount, mentions: mentions };
})();
