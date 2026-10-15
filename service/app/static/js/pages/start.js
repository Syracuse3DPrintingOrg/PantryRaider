// Start page (templates/start.html): the Glance hero and pills, the key
// grid actions, and the live key faces. Server data arrives in the
// #start-data JSON block.

const START_DATA = JSON.parse(document.getElementById('start-data').textContent);

// Glance landing only.
if (START_DATA.glance) {
  prOnReady(function () {
    // Time-of-day greeting in the hero eyebrow, from the kiosk's own clock.
    const greet = document.getElementById('glance-greeting');
    if (greet) {
      const h = new Date().getHours();
      greet.textContent = h < 5 ? 'Late night in the kitchen'
        : h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
    }
    // Glance notification pills (FoodAssistant-gg33): each pill polls a count
    // endpoint and shows the number when > 0, or a calm check when all clear.
    // Uses the same staggered, pause-when-hidden poll pattern as the live key
    // faces so a background kiosk tab stops hammering the endpoints.
    const pills = Array.from(document.querySelectorAll('.glance-pill[data-endpoint]'));
    if (!pills.length) return;
    function url(path) { return new URL(path, document.baseURI).href; }
    function render(pill, count) {
      const slot = pill.querySelector('[data-count-slot]');
      if (!slot) return;
      if (count > 0) {
        pill.classList.add('has-count');
        slot.classList.remove('pill-clear');
        slot.textContent = count > 99 ? '99+' : String(count);
      } else {
        pill.classList.remove('has-count');
        slot.classList.add('pill-clear');
        slot.innerHTML = '<i class="bi bi-check2"></i>';
      }
    }
    function poll(pill) {
      if (document.hidden) return;
      fetch(url(pill.dataset.endpoint))
        .then(r => r.json())
        .then(d => { if (d && typeof d.count === 'number') render(pill, d.count); })
        .catch(() => {});  // leave the last good count on a transient error
    }
    // Which status count feeds each pill, so the consolidated poll can render
    // them without a fetch per pill. The data-endpoint the legacy fetch uses
    // stays on the element (labels, navigation) and names the same number.
    function pillCount(pill, counts) {
      const ep = pill.dataset.endpoint || '';
      if (ep.indexOf('action-items/count') === 0) return counts.actions;
      if (ep.indexOf('events/count') === 0) return counts.alerts;
      if (ep.indexOf('expiring/count') === 0) return counts.expiring;
      return undefined;
    }
    function startLegacyPills() {
      // Stagger the first fetches so they do not all land at once, then
      // refresh each on a slow cadence (these counts change on the order of
      // minutes).
      pills.forEach((pill, i) => {
        setTimeout(() => { poll(pill); setInterval(() => poll(pill), 60 * 1000); }, 250 + i * 250);
      });
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) pills.forEach(poll);
      });
    }

    // Live tile footers (Dan's smart-home-OS direction): a few tiles carry a
    // real reading under their hairline, like a device status line. Same
    // gentle staggered cadence; a tile with nothing to say keeps its slot
    // hidden so the launcher never shows a dead placeholder.
    function tileSlot(key) {
      const t = document.querySelector('.glance-card[data-tile="' + key + '"] [data-live-slot]');
      return t || null;
    }
    function setLive(key, text, hot) {
      const slot = tileSlot(key);
      if (!slot) return;
      slot.textContent = text; slot.hidden = !text;
      slot.classList.toggle('live-hot', !!hot);
    }
    function pollCounts() {
      if (document.hidden) return;
      fetch(url('expiring/count?days=7')).then(r => r.json()).then(d => {
        if (d && typeof d.count === 'number') {
          setLive('inventory', d.count > 0 ? d.count + ' expiring this week' : '', true);
        }
      }).catch(() => {});
      fetch(url('action-items/count')).then(r => r.json()).then(d => {
        if (d && typeof d.count === 'number') {
          setLive('pending', d.count > 0 ? d.count + ' waiting for you' : '', true);
        }
      }).catch(() => {});
    }
    // The Time & Temp tile counts down its soonest timer for real, ticking
    // locally between polls off the server's absolute remaining seconds.
    let timerBase = null, timerAt = 0;
    function fmtRemain(s) {
      s = Math.max(0, Math.round(s));
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }
    function pollTimersTile() {
      if (document.hidden) return;
      fetch(url('timers')).then(r => r.json()).then(d => {
        const ts = (d && d.timers) || [];
        const running = ts.filter(t => !t.expired && typeof t.remaining_seconds === 'number')
          .sort((a, b) => a.remaining_seconds - b.remaining_seconds);
        timerBase = running.length ? { label: running[0].label, remain: running[0].remaining_seconds } : null;
        timerAt = Date.now();
        renderTimerTile();
      }).catch(() => {});
    }
    function renderTimerTile() {
      if (!timerBase) { setLive('timetemp', '', false); return; }
      const left = timerBase.remain - (Date.now() - timerAt) / 1000;
      if (left <= 0) { setLive('timetemp', timerBase.label + ' done', true); return; }
      setLive('timetemp', timerBase.label + ' ' + fmtRemain(left), true);
    }
    // Prefer the consolidated kiosk poll: one subscription renders the pills,
    // the live tile footers, and the Time & Temp timer tile from the merged
    // status. The 1s timer redraw stays local (never a fetch). Fall back to
    // the per-surface staggered fetches without the shared loop.
    if (window.PRKioskStatus) {
      window.PRKioskStatus.subscribe((s) => {
        if (!s) return;
        const c = s.counts || {};
        pills.forEach((pill) => {
          const n = pillCount(pill, c);
          if (typeof n === 'number') render(pill, n);
        });
        if (typeof c.expiring === 'number') {
          setLive('inventory', c.expiring > 0 ? c.expiring + ' expiring this week' : '', true);
        }
        if (typeof c.actions === 'number') {
          setLive('pending', c.actions > 0 ? c.actions + ' waiting for you' : '', true);
        }
        if (s.timers && Array.isArray(s.timers.timers)) {
          const running = s.timers.timers
            .filter(t => !t.expired && typeof t.remaining_seconds === 'number')
            .sort((a, b) => a.remaining_seconds - b.remaining_seconds);
          timerBase = running.length ? { label: running[0].label, remain: running[0].remaining_seconds } : null;
          timerAt = Date.now();
          renderTimerTile();
        }
      }, { interval: 15000, wants: ['expiring'] });
      setInterval(renderTimerTile, 1000);  // smooth local countdown between polls
    } else {
      startLegacyPills();
      setTimeout(() => { pollCounts(); setInterval(pollCounts, 60 * 1000); }, 1200);
      setTimeout(() => { pollTimersTile(); setInterval(pollTimersTile, 15 * 1000); setInterval(renderTimerTile, 1000); }, 700);
    }
  });
}

prOnReady(function () {
  // Where a shared custom button sends you on-screen. The action-typed deck
  // buttons (HA toggle, media, macro) and the built-in HA slot keys fire
  // server-side via ui/start/fire, so they work without a connected deck.
  const ROUTE = { shopping_add: 'ui/shopping',
    weather: 'ui/weather', camera: 'ui/camera' };
  const FIRE = { ha_action: 1, media: 1, macro: 1, timer: 1 };
  // Built-in timer keys start/advance the shared timer instead of just
  // opening the Timers page, matching the deck key (FoodAssistant-6ifu).
  // The timers_view key still navigates.
  const FIRE_KEYS = { timer_1: 1, timer_2: 1, timer_3: 1,
    timer_eggs: 1, timer_pasta: 1, timer_rice: 1 };
  function toast(msg) {
    const t = document.getElementById('start-toast');
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = 'none'; }, 2200);
  }
  // Resolve a relative app path against the <base href>, not the current
  // /ui/start URL. Setting location.href on a relative value resolves it
  // against the page URL, which would make "ui/" become "/ui/ui/" and the
  // key appear to do nothing (Pantry Raider). Only an http(s) result is
  // followed, so a key can never run a javascript: address; this page
  // does not load base-page.js, so it carries its own copy of PRSafeHref.
  function safeHref(path) {
    if (window.PRSafeHref) return window.PRSafeHref(path);
    const raw = String(path == null ? '' : path).trim();
    if (!raw) return null;
    try {
      const u = new URL(raw, document.baseURI);
      return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
    } catch (e) { return null; }
  }
  function go(path) { const u = safeHref(path); if (u) location.href = u; }
  function fireServer(name, long) {
    const url = new URL('ui/start/fire/' + encodeURIComponent(name), document.baseURI);
    if (long) url.searchParams.set('long', 'true');
    fetch(url, { method: 'POST' })
      .then(r => r.json())
      .then(d => { toast(d.detail || (d.ok ? 'Done.' : 'That did not work.')); pollTimers(); })
      .catch(() => toast('Could not reach the server.'));
  }

  // Live timer faces: keys that can start a timer adopt the matching
  // shared-registry timer by label (the deck does the same), show its
  // remaining time ticking locally from deadline_epoch, and pulse when
  // done until a press dismisses it.
  const TIMER_LABELS = { timer_1: 'Timer 1', timer_2: 'Timer 2', timer_3: 'Timer 3',
    timer_eggs: 'Eggs', timer_pasta: 'Pasta', timer_rice: 'Rice' };
  const timerKeys = [];  // { el, label, orig: {icoHTML, lbl} }
  document.querySelectorAll('.start-key[role="button"]').forEach(el => {
    let label = null;
    if (el.dataset.kind === 'builtin' && TIMER_LABELS[el.dataset.key]) label = TIMER_LABELS[el.dataset.key];
    if (el.dataset.kind === 'custom' && el.dataset.timerLabel) label = el.dataset.timerLabel;
    if (label) {
      timerKeys.push({ el, label,
        orig: { ico: el.querySelector('.ico').outerHTML, lbl: el.querySelector('.lbl').textContent } });
    }
  });
  let liveTimers = [];
  function mmss(sec) {
    sec = Math.max(0, Math.round(sec));
    return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  }
  function renderTimerKeys() {
    const now = Date.now() / 1000;
    timerKeys.forEach(k => {
      const t = liveTimers.find(x => x.label === k.label);
      if (!t) {
        if (k.el.classList.contains('timer-running') || k.el.classList.contains('timer-done')) {
          k.el.classList.remove('timer-running', 'timer-done');
          k.el.innerHTML = k.orig.ico + '<span class="lbl">' + k.orig.lbl + '</span>';
        }
        return;
      }
      const remaining = t.deadline_epoch - now;
      const done = remaining <= 0;
      k.el.classList.toggle('timer-running', !done);
      k.el.classList.toggle('timer-done', done);
      k.el.innerHTML = '<span class="countdown">' + (done ? 'Done' : mmss(remaining)) + '</span>' +
                       '<span class="lbl">' + k.orig.lbl + '</span>';
    });
  }
  function applyLiveTimers(list) { liveTimers = list || []; renderTimerKeys(); }
  function pollTimers() {
    if (!timerKeys.length) return;
    // Hidden (background tab): the local render tick keeps the visible
    // countdown honest from deadline_epoch; resync when shown again.
    if (document.hidden) return;
    fetch(new URL('timers', document.baseURI))
      .then(r => r.json())
      .then(d => applyLiveTimers((d && d.timers) || d))
      .catch(() => {});
  }
  if (timerKeys.length) {
    setInterval(renderTimerKeys, 1000);  // smooth local countdown between polls
    // Prefer the consolidated kiosk poll (the deck keys' 3s cadence is the
    // fastest field it carries); fall back to the dedicated 3s timers poll
    // without the shared loop.
    if (window.PRKioskStatus) {
      window.PRKioskStatus.subscribe((s) => {
        if (s && s.timers && Array.isArray(s.timers.timers)) applyLiveTimers(s.timers.timers);
      }, { interval: 3000 });
    } else {
      pollTimers();
      setInterval(pollTimers, 3000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) pollTimers();
      });
    }
  }
  function fire(el) {
    if (el.dataset.kind === 'builtin') {
      if (FIRE_KEYS[el.dataset.key]) { fireServer(el.dataset.key); return; }
      go(el.dataset.href); return;
    }
    if (el.dataset.kind === 'deckonly') {
      // The built-in HA slot keys (ha_1..ha_5) execute server-side; the
      // rest (paging, brightness, deck workflow) really do need hardware.
      if (/^ha_[1-5]$/.test(el.dataset.key || '')) { fireServer(el.dataset.key); return; }
      toast('"' + (el.dataset.label || 'This') + '" runs on a connected Stream Deck.'); return;
    }
    const t = el.dataset.type;
    if (ROUTE[t]) { go(ROUTE[t]); return; }
    if (FIRE[t]) { fireServer(el.dataset.id); return; }
    go('ui/');
  }
  // Long press on a timer key resets it, like holding the deck key
  // (FoodAssistant-b8o1). Pointer events cover touch and mouse; a long
  // press swallows the click that follows so it cannot also short-press.
  const LONG_MS = 550;
  function timerName(el) {
    if (el.dataset.kind === 'builtin' && FIRE_KEYS[el.dataset.key]) return el.dataset.key;
    if (el.dataset.kind === 'custom' && el.dataset.type === 'timer') return el.dataset.id;
    return null;
  }
  document.querySelectorAll('.start-key[role="button"]').forEach(el => {
    const tname = timerName(el);
    if (tname) {
      let holdT = null;
      el.addEventListener('pointerdown', () => {
        holdT = setTimeout(() => {
          holdT = null;
          el._longFired = true;
          fireServer(tname, true);
        }, LONG_MS);
      });
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(evn =>
        el.addEventListener(evn, () => { if (holdT) { clearTimeout(holdT); holdT = null; } }));
    }
    el.addEventListener('click', () => {
      if (el._longFired) { el._longFired = false; return; }
      fire(el);
    });
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(el); } });
  });
});

prOnReady(function () {
  // Live built-in key faces (FoodAssistant-x2u7): weather, forecast, the
  // expiring count, and a camera snapshot preview, mirroring the Stream
  // Deck. Every fetch failure leaves a clean static fallback face; nothing
  // here throws in a way that can block the grid or the timer loop above.
  const WX_UNITS = (String(START_DATA.weather_units || '').toLowerCase() === 'c') ? 'C' : 'F';
  const wx = { weather: null, forecast: null };
  const expiringKeys = [];
  const cameraKeys = [];
  document.querySelectorAll('.start-key[data-live]').forEach(el => {
    // Remember the stock icon+label face so any failure restores it cleanly.
    el._origFace = el.innerHTML;
    const kind = el.dataset.live;
    if (kind === 'weather') wx.weather = el;
    else if (kind === 'forecast') wx.forecast = el;
    else if (kind === 'expiring') expiringKeys.push(el);
    else if (kind === 'camera') cameraKeys.push(el);
  });
  function url(path) { return new URL(path, document.baseURI).href; }
  function restore(el) {
    el.classList.remove('live-muted', 'expiring-alert', 'has-snapshot');
    el.style.backgroundImage = '';
    if (el._origFace != null && el.innerHTML !== el._origFace) el.innerHTML = el._origFace;
  }

  // -- weather + forecast (one shared fetch feeds both keys) ------------
  const hasWx = !!(wx.weather || wx.forecast);
  // The credit is required wherever the data is shown, so it follows the
  // same condition the fetch does rather than a separate setting.
  if (hasWx) { const c = document.getElementById('wx-credit'); if (c) c.hidden = false; }
  function wxNoSignal(el) {
    el.classList.add('live-muted');
    el.classList.remove('has-snapshot');
    el.innerHTML = '<i class="bi bi-cloud-slash ico"></i><span class="lbl">No signal</span>';
  }
  function renderWeather(fc) {
    const cur = (fc && fc.current) || {};
    if (wx.weather) {
      const temp = cur.temp;
      if (temp === undefined || temp === null || temp === '?') { wxNoSignal(wx.weather); }
      else {
        wx.weather.classList.remove('live-muted');
        const desc = String(cur.desc || '').trim();
        wx.weather.innerHTML = '<span class="live-big">' + PR.esc(temp) + '°' + WX_UNITS + '</span>' +
          '<span class="live-sub">' + (PR.esc(desc) || 'Weather') + '</span>';
      }
    }
    if (wx.forecast) {
      const day = (fc && fc.days && fc.days[0]) || null;
      if (!day || day.hi === undefined) { wxNoSignal(wx.forecast); }
      else {
        wx.forecast.classList.remove('live-muted');
        const lo = (day.lo === undefined || day.lo === null) ? '?' : day.lo;
        wx.forecast.innerHTML = '<span class="live-big">' + PR.esc(day.hi) + '°</span>' +
          '<span class="live-sub">Low ' + PR.esc(lo) + '°</span>';
      }
    }
  }
  function pollWeather() {
    if (!hasWx || document.hidden) return;
    fetch(url('ui/weather/data'))
      .then(r => r.json())
      .then(d => {
        if (d && d.ok && d.forecast) renderWeather(d.forecast);
        else { if (wx.weather) wxNoSignal(wx.weather); if (wx.forecast) wxNoSignal(wx.forecast); }
      })
      .catch(() => {});  // keep the last good face on a transient error
  }

  // -- expiring count --------------------------------------------------
  function renderExpiring(count) {
    expiringKeys.forEach(el => {
      if (count > 0) {
        el.classList.add('expiring-alert');
        el.innerHTML = '<span class="live-big">' + PR.esc(count) + '</span>' +
          '<span class="live-sub">Expiring</span>';
      } else {
        restore(el);  // calm state: back to the stock icon face
      }
    });
  }
  function pollExpiring() {
    if (!expiringKeys.length || document.hidden) return;
    fetch(url('expiring/count'))
      .then(r => r.json())
      .then(d => { if (d && typeof d.count === 'number') renderExpiring(d.count); })
      .catch(() => {});
  }

  // -- camera snapshot preview ----------------------------------------
  function pollCamera() {
    if (!cameraKeys.length || document.hidden) return;
    cameraKeys.forEach(el => {
      const idx = el.dataset.camIndex || '0';
      const src = url('ui/camera/' + encodeURIComponent(idx) + '/snapshot') + '?t=' + Date.now();
      const img = new Image();
      img.onload = () => {
        el.classList.add('has-snapshot');
        el.style.backgroundImage = 'url("' + src + '")';
      };
      img.onerror = () => { el.classList.remove('has-snapshot'); el.style.backgroundImage = ''; };
      img.src = src;
    });
  }

  // Stagger the first fetches so they do not all land at once, then poll
  // each on a cadence sized to how fast its data changes. Every poll is
  // skipped while the tab is hidden (the guards above) and kicked once on
  // return, so a background kiosk tab stops hammering the endpoints.
  function start(fn, delay, every) { setTimeout(() => { fn(); setInterval(fn, every); }, delay); }
  if (hasWx) start(pollWeather, 200, 10 * 60 * 1000);          // weather: every 10 min
  // The expiring count rides the consolidated kiosk poll when present (one
  // request feeds every count on the page); otherwise its own 2-min poll.
  // Weather and the camera snapshot stay their own polls: weather is a rare
  // 10-min pull, and a snapshot is image bytes, not a status field.
  const sharedExpiring = !!(expiringKeys.length && window.PRKioskStatus);
  if (sharedExpiring) {
    window.PRKioskStatus.subscribe((s) => {
      if (s && s.counts && typeof s.counts.expiring === 'number') renderExpiring(s.counts.expiring);
    }, { interval: 120000, wants: ['expiring'] });
  } else if (expiringKeys.length) {
    start(pollExpiring, 600, 2 * 60 * 1000);  // count: every 2 min
  }
  if (cameraKeys.length) start(pollCamera, 1000, 20 * 1000);         // snapshot: every 20 s
  if (hasWx || expiringKeys.length || cameraKeys.length) {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      pollWeather(); pollCamera();
      if (!sharedExpiring) pollExpiring();
    });
  }
});
