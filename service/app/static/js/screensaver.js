// Kiosk screensaver (FoodAssistant-y65x, photos FoodAssistant-5w4m).
//
// Runs in kiosk mode, or in ANY browser viewing the install when the
// "screensaver on every browser" setting is on (data-all-clients,
// FoodAssistant-xlb3). After the configured idle minutes the page fades to
// a near-black overlay, and any touch or key press dismisses it instantly.
// This is the SOFT layer for panels where full display blanking is unwanted:
// the separate "Display sleep" setting powers the panel itself off via the
// host bridge, while this one keeps the display lit and just covers the page.
//
// Styles, chosen by the per-device screensaver_mode setting:
//   bounce    the Pantry Raider logo gliding around the screen with the clock
//             riding under it (the default; constant motion is the burn-in guard)
//   photos    a slideshow from the configured photo source (an attached USB
//             drive, a folder on the server, an Immich album, or direct image
//             links; FoodAssistant-5w4m, af1l), cover-fit with a slow Ken
//             Burns drift and a crossfade every 25 seconds; a small clock hops
//             corners between photos so nothing sits still. An empty source or
//             a failed fetch falls back to the bounce style, so the setting is
//             always safe to leave on.
//   toasters  the After Dark flying-toasters homage (FoodAssistant-umnk): winged
//             chrome toasters and slices of toast fly from the upper right toward
//             the lower left across a black screen, in three parallax layers
//             (nearer ones bigger and faster), wings flapping, with the odd
//             brand-pink toaster as the Pantry Raider wink. Every sprite is drawn
//             programmatically on a canvas: no image files, works offline.
//   starfield stars streaming out from the centre at warp, the classic space
//             saver, also pure canvas. An occasional star is brand pink.
// The two canvas modes share startCanvasMode(): one requestAnimationFrame loop
// draws the sprites AND steps the timer pills, sprite counts are capped for a
// Pi 3, no per-frame allocation, and prefers-reduced-motion thins and slows the
// field rather than strobing. A hopping corner clock is the burn-in guard, and
// any draw error tears the canvas down and falls back to the bounce style, so
// an unknown or broken mode is never a blank screen. An unknown mode string is
// normalized to bounce up front (normalizeMode), mirroring the server-side
// SCREENSAVER_MODES fallback.
//
// The timeout comes from #screensaver-config (data-minutes, rendered from the
// per-device saved setting); 0 or a missing config disables idle activation.
// The settings page Test button (FoodAssistant-fiwc) forces the saver on
// through window.__screensaverTest regardless of kiosk mode or the timeout,
// so choices can be previewed without waiting for the idle countdown.
//
// Idle activation is suppressed while a camera is on screen
// (FoodAssistant-ysf6): the camera page or an ha-events camera pop-up means
// someone is watching a feed, which is intentionally idle.
//
// Floating kitchen timers (FoodAssistant-8c6m): while the saver is up, every
// active timer from the shared /timers registry rides the screen as a small
// bouncing pill (label + live countdown) in BOTH styles. Pills reflect off
// the panel edges with the same zoom-aware layout-unit walls the logo uses,
// bounce off each other with simple equal-mass elastic collisions, and in
// bounce mode also carom off the logo block (which never changes course, so
// the classic straight-line glide survives). The registry is polled every few
// seconds; between polls each pill counts down locally from deadline_epoch,
// the same shareable formula the Stream Deck uses. In a countdown's last
// minute the pill's face hops gently, and a finished timer pulses red/amber
// with a Done readout while the pill turns slowly, so both stages read from
// across the kitchen; the stage animations are CSS on the pill's inner face,
// so the physics body underneath never changes course. At most
// six pills are simulated (Pi 3 budget); the rest collapse into a static
// "+N more" pill.
(function () {
  var kiosk = false;
  try {
    kiosk = localStorage.getItem('kioskMode') === 'true';
  } catch (e) { /* no storage / private mode: idle activation never runs */ }

  var cfg = document.getElementById('screensaver-config');
  if (!cfg) return;
  var minutes = parseInt(cfg.getAttribute('data-minutes') || '0', 10);
  // "Screensaver on every browser": idle activation stops being kiosk-only.
  var ALL_CLIENTS = cfg.getAttribute('data-all-clients') === 'true';

  var IDLE_MS = (minutes > 0 ? minutes : 0) * 60 * 1000;
  // Glide speed in pixels per second, from the per-device setting.
  var SPEEDS = { slow: 18, normal: 32, fast: 60 };
  var SPEED = SPEEDS[cfg.getAttribute('data-speed') || 'normal'] || SPEEDS.normal;
  // Known styles; an unknown value falls back to the bouncing logo, mirroring
  // the server-side SCREENSAVER_MODES fallback so a stray setting is never a
  // blank screen. Exposed for the tests.
  var SCREENSAVER_MODES = ['bounce', 'photos', 'toasters', 'starfield'];
  function normalizeMode(m) {
    return SCREENSAVER_MODES.indexOf(m) !== -1 ? m : 'bounce';
  }
  window.__screensaverMode = normalizeMode;
  var MODE = normalizeMode(cfg.getAttribute('data-mode'));
  // Timer pill size multiplier, from the per-device setting: small panels
  // viewed across a kitchen want bigger countdowns.
  var PILL_SCALES = { normal: 1, large: 1.35, xlarge: 1.7 };
  var PILL_K = PILL_SCALES[cfg.getAttribute('data-pill-scale') || 'normal'] || 1;
  function pv(n) { return (n * PILL_K).toFixed(2) + 'vmin'; }
  var _ps = parseInt(cfg.getAttribute('data-photo-seconds'), 10);
  // How long each slideshow photo stays up, clamped to a sane range.
  var PHOTO_MS = (isNaN(_ps) ? 25 : Math.max(2, Math.min(120, _ps))) * 1000;
  // Ken Burns pan/zoom drift on each photo (on unless explicitly disabled).
  var KEN_BURNS = cfg.getAttribute('data-ken-burns') !== 'false';
  // Someone who asked the OS for less motion gets a still slideshow: the
  // crossfade stays, the pan/zoom does not.
  var REDUCED_MOTION = false;
  try {
    REDUCED_MOTION = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { /* no matchMedia: treat as full motion */ }
  // How far the Ken Burns drift travels: a magnitude multiplier per speed.
  // The pan was still reading as very gentle across a kitchen, so it now moves
  // noticeably more at every speed and the range is wider: slow is a calm
  // drift, normal has real motion, and fast is a bold sweep across the photo
  // (FoodAssistant-jcr6). More pan needs more zoom-in to keep the frame
  // covered, so a bolder speed also crops in a little more.
  var KEN_BURNS_MAG = { slow: 0.6, normal: 1.1, fast: 2.0 };
  var KEN_BURNS_SPEED =
    cfg.getAttribute('data-ken-burns-speed') || 'normal';

  // Pure helper: pick the start scale, end scale, and max pan (in percent of
  // the frame) for a given Ken Burns speed. The frame must stay covered at
  // every moment: the transform is scale(S) translate(P%), so a P% pan shifts
  // the frame by S*(P/100) of its width while each scaled edge only overhangs
  // (S-1)/2, which needs S >= 1/(1 - 2*P/100). Exposed for the tests.
  function kenBurnsFrame(speed) {
    var mag = KEN_BURNS_MAG[speed] || KEN_BURNS_MAG.normal;
    var pan = 9 * mag;                 // max offset from centre, percent
    var zoomTravel = 0.10 * mag;       // how much the scale grows over the shot
    var minScale = 1 / (1 - 2 * (pan / 100)) + 0.01;  // keep the frame covered
    var startScale = Math.max(1.08, minScale);
    return { startScale: startScale, endScale: startScale + zoomTravel, pan: pan };
  }
  window.__kenBurnsFrame = kenBurnsFrame;
  var FADE_MS = 2000;     // crossfade length between photos
  var lastActivity = Date.now();
  var overlay = null;
  var clockTimer = null;
  var rafId = null;
  var photoTimer = null;
  var canvasCleanup = null;   // tears down a canvas mode's clock-hop interval

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // 12/24-hour clock reading, from the fleet-synced setting stamped on <html>
  // by base.html. '12' shows 3:42 with a small AM/PM; '24' and 'auto' (the
  // long-standing default look) show 15:42.
  var CLOCK_FORMAT =
    document.documentElement.getAttribute('data-clock-format') || 'auto';

  function updateClock() {
    if (!overlay) return;
    var now = new Date();
    var t = overlay.querySelector('.ss-time');
    var d = overlay.querySelector('.ss-date');
    if (t) {
      if (CLOCK_FORMAT === '12') {
        var h = now.getHours();
        t.textContent = (h % 12 || 12) + ':' + pad(now.getMinutes());
        var ampm = document.createElement('span');
        ampm.className = 'ss-ampm';
        ampm.style.cssText =
          'font-size:0.42em;font-weight:600;margin-left:0.35em;opacity:0.8;';
        ampm.textContent = h < 12 ? 'AM' : 'PM';
        t.appendChild(ampm);
      } else {
        t.textContent = pad(now.getHours()) + ':' + pad(now.getMinutes());
      }
    }
    if (d) d.textContent = now.toLocaleDateString(undefined, {
      weekday: 'long', month: 'long', day: 'numeric',
    });
  }

  // Corner clock placement for the photo and canvas savers. A right- or
  // bottom-anchored element has to stay fully on-screen at every display
  // rotation: on a 270-rotated panel the clock was clipping off the right edge
  // (FoodAssistant-7irg). The clock is capped so it can never grow wider than
  // the panel (max-width accounts for its own width) and sits a safe inset in
  // from the chosen corner, so its box always lands inside the visible area.
  // Pure string builder, exposed for the tests. `corner` is the anchor side(s),
  // e.g. 'right:3vmin;bottom:3vmin;'.
  function cornerClockCss(corner) {
    return 'position:absolute;' + corner + 'z-index:3;font-size:3.5vmin;' +
      'font-weight:600;color:#e8eaed;text-shadow:0 0 1.2vmin rgba(0,0,0,0.9);' +
      'max-width:calc(100% - 8vmin);white-space:nowrap;' +
      'overflow:hidden;text-overflow:clip;';
  }
  window.__cornerClockCss = cornerClockCss;

  // The layout-pixel viewport, the space translate() coordinates render in:
  // the visual viewport divided by the kiosk interface scale's zoom
  // (kiosk-display.js sets html.style.zoom; 1 everywhere else). Read per
  // frame so a live scale change just tightens the walls without a jump.
  // Mixing window.innerWidth (visual pixels) with translate coordinates
  // (layout pixels) broke the walls whenever a zoom applied
  // (FoodAssistant-vf4f), so every wall below comes through here.
  function layoutSize() {
    var z = 1;
    try {
      z = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
    } catch (e) { /* keep 1 */ }
    return { w: window.innerWidth / z, h: window.innerHeight / z };
  }

  // Old-school DVD bounce: the block travels in a dead-straight line at
  // constant speed until it HITS an edge, then reflects (angle in = angle
  // out) and carries on; nothing else ever changes its course. Frame-time
  // based so the speed is identical on a slow Pi and a fast desktop, and the
  // block size is measured each frame so a viewport resize or font load just
  // tightens the walls without a jump. Transform keeps motion compositor-side.
  //
  // Every measurement here lives in LAYOUT pixels: layoutSize() for the
  // walls (the vf4f zoom fix) and offsetWidth/Height for the block.
  function startBounce(block) {
    var x = null, y = null, dx = 0, dy = 0, last = null;
    bounceActive = true;  // this loop drives the timer pills too
    function step(ts) {
      if (!overlay) return;
      var vp = layoutSize();
      var w = vp.w, h = vp.h;
      var bw = block.offsetWidth, bh = block.offsetHeight;
      var maxX = Math.max(0, w - bw);
      var maxY = Math.max(0, h - bh);
      if (x === null) {
        x = Math.random() * Math.max(0, w - bw);
        y = Math.random() * Math.max(0, h - bh);
        // A fixed 30-60 degree launch keeps the path visibly diagonal (the
        // classic look) and never so flat that one axis barely moves.
        var ang = (30 + Math.random() * 30) * Math.PI / 180;
        dx = (Math.random() < 0.5 ? -1 : 1) * Math.cos(ang);
        dy = (Math.random() < 0.5 ? -1 : 1) * Math.sin(ang);
      }
      if (last !== null) {
        var dt = Math.min(100, ts - last) / 1000;
        x += dx * SPEED * dt;
        y += dy * SPEED * dt;
        // Reflect exactly at the wall: place the block ON the edge for the
        // corner-kiss moment, flip only the axis that hit.
        if (x <= 0) { x = 0; dx = Math.abs(dx); }
        else if (x >= maxX) { x = maxX; dx = -Math.abs(dx); }
        if (y <= 0) { y = 0; dy = Math.abs(dy); }
        else if (y >= maxY) { y = maxY; dy = -Math.abs(dy); }
      }
      last = ts;
      block.style.transform = 'translate(' + x + 'px,' + y + 'px)';
      // The timer pills ride this same frame step (one rAF loop total). They
      // carom off the logo block, approximated as a circle in on-screen
      // coordinates; the logo itself never changes course.
      stepTimerBodies(ts, w, h, {
        cx: x + bw / 2, cy: y + bh / 2, r: (bw + bh) / 4,
      });
      rafId = requestAnimationFrame(step);
    }
    rafId = requestAnimationFrame(step);
  }

  // -- floating kitchen timers (FoodAssistant-8c6m) -------------------------

  var TIMER_POLL_MS = 5000;   // registry poll cadence while the saver is up
  var TIMER_CAP = 6;          // Pi 3 budget: simulate at most this many pills
  var DONE_SPEEDUP = 1.35;    // a finished timer drifts a little faster
  var timerPollId = null;
  var timerRafId = null;      // photos-mode loop; bounce mode rides step()
  var timerLast = null;       // last physics timestamp, for dt
  var timerBodies = [];       // simulated pills, positions in layout px
  var timerOverflow = null;   // the static "+N more" pill
  var bounceActive = false;   // true while the logo loop is driving physics

  // Food icon for a timer label: pure keyword-to-emoji lookup, so a "Pasta"
  // timer reads as pasta from across the room. Labels are tokenized on
  // non-letters, lowercased, and a trailing s is tried singular (Eggs, Wings);
  // the first token with a mapping wins. No food word gets the stopwatch.
  var TIMER_FOOD_ICONS = {
    egg: '\u{1F95A}',
    pasta: '\u{1F35D}', noodle: '\u{1F35D}', spaghetti: '\u{1F35D}',
    macaroni: '\u{1F35D}',
    rice: '\u{1F35A}',
    pizza: '\u{1F355}',
    bread: '\u{1F35E}', dough: '\u{1F35E}', loaf: '\u{1F35E}',
    toast: '\u{1F35E}',
    chicken: '\u{1F357}', wing: '\u{1F357}',
    beef: '\u{1F969}', steak: '\u{1F969}',
    pork: '\u{1F953}', bacon: '\u{1F953}',
    fish: '\u{1F41F}', salmon: '\u{1F41F}',
    shrimp: '\u{1F364}',
    soup: '\u{1F372}', stew: '\u{1F372}', simmer: '\u{1F372}',
    sauce: '\u{1F372}',
    tea: '\u{1F375}',
    coffee: '☕',
    cookie: '\u{1F36A}',
    cake: '\u{1F9C1}', muffin: '\u{1F9C1}',
    pie: '\u{1F967}',
    potato: '\u{1F954}',
    corn: '\u{1F33D}',
    veggie: '\u{1F966}', broccoli: '\u{1F966}',
    turkey: '\u{1F983}',
    lamb: '\u{1F356}',
    // Looser bucket: the label names the cooking method, not the food.
    oven: '\u{1F525}', roast: '\u{1F525}', bake: '\u{1F525}',
    broil: '\u{1F525}', grill: '\u{1F525}',
  };
  var TIMER_DEFAULT_ICON = '⏱️';  // stopwatch: not obviously food

  function timerFoodIcon(label) {
    var tokens = String(label || '').toLowerCase().split(/[^a-z]+/);
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (!t) continue;
      if (!Object.prototype.hasOwnProperty.call(TIMER_FOOD_ICONS, t) &&
          t.length > 3 && t.charAt(t.length - 1) === 's') {
        t = t.slice(0, -1);  // Eggs -> egg, Wings -> wing
      }
      if (Object.prototype.hasOwnProperty.call(TIMER_FOOD_ICONS, t)) {
        return TIMER_FOOD_ICONS[t];
      }
    }
    return TIMER_DEFAULT_ICON;
  }
  // Test-only: lets the automated probe assert the label-to-icon mapping.
  window.__timerFoodIcon = timerFoodIcon;

  function fmtRemaining(s) {
    s = Math.max(0, Math.ceil(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return (h > 0 ? h + ':' + pad(m) : m) + ':' + pad(s % 60);
  }

  // A pill's collision radius: the average of its half-width and half-height.
  // The circle approximation is deliberately loose; it just has to look right
  // at glide speed.
  function timerRadius(b) { return (b.w + b.h) / 4; }

  function makeTimerPill(t) {
    // Two layers on purpose: the outer shell only ever carries the physics
    // translate(), so its layout box stays the deterministic collision body,
    // while the inner face holds the pill visuals and any CSS animation (the
    // last-minute hop, the finished spin). Animating the face never bends the
    // drift underneath.
    var el = document.createElement('div');
    el.className = 'ss-timer';
    el.style.cssText =
      'position:absolute;left:0;top:0;z-index:4;will-change:transform;';
    var face = document.createElement('div');
    face.className = 'ss-timer-face';
    face.style.cssText =
      'display:flex;align-items:center;gap:' + pv(1.4) + ';padding:' + pv(1.1) + ' ' + pv(2.4) + ';' +
      'border-radius:999px;background:rgba(24,26,32,0.88);' +
      'border:1px solid rgba(255,255,255,0.18);color:#e8eaed;' +
      'white-space:nowrap;';
    var iconChar = timerFoodIcon(t.label);
    var icon = document.createElement('span');
    icon.className = 'ss-timer-icon';
    icon.style.cssText = 'font-size:' + pv(4.2) + ';line-height:1;';
    icon.textContent = iconChar;
    var col = document.createElement('div');
    col.style.cssText = 'text-align:left;';
    var lab = document.createElement('div');
    lab.className = 'ss-timer-label';
    lab.style.cssText = 'font-size:' + pv(2.1) + ';opacity:0.75;max-width:' + pv(34) + ';' +
      'overflow:hidden;text-overflow:ellipsis;';
    lab.textContent = t.label || 'Timer';
    var time = document.createElement('div');
    time.className = 'ss-timer-time';
    time.style.cssText = 'font-size:' + pv(3.4) + ';font-weight:600;line-height:1.15;' +
      'font-variant-numeric:tabular-nums;';
    col.appendChild(lab);
    col.appendChild(time);
    face.appendChild(icon);
    face.appendChild(col);
    el.appendChild(face);
    return { el: el, iconEl: icon, labelEl: lab, timeEl: time, iconChar: iconChar };
  }

  // A finished timer (expired, still listed until dismissed) must read from
  // across the kitchen: the pill pulses red/amber and turns slowly (the
  // ss-timer-done rules injected in show()), the countdown swaps to "Done",
  // the food icon stays, and the drift picks up a little. The last-minute hop
  // hands off to the spin here.
  function markTimerDone(b) {
    if (b.done) return;
    b.done = true;
    b.el.classList.remove('ss-timer-ending');
    b.el.classList.add('ss-timer-done');
    b.timeEl.textContent = 'Done';
    b.vx *= DONE_SPEEDUP;
    b.vy *= DONE_SPEEDUP;
  }

  function spawnTimerBody(t, w, h) {
    var parts = makeTimerPill(t);
    overlay.appendChild(parts.el);
    var pw = parts.el.offsetWidth || 1, ph = parts.el.offsetHeight || 1;
    // A handful of random tries for a spot clear of the other pills; if the
    // screen is crowded the collision solver separates whatever overlaps.
    var x = 0, y = 0;
    for (var tries = 0; tries < 24; tries++) {
      x = Math.random() * Math.max(0, w - pw);
      y = Math.random() * Math.max(0, h - ph);
      var clear = true;
      for (var i = 0; i < timerBodies.length; i++) {
        var o = timerBodies[i];
        var dx = (x + pw / 2) - (o.x + o.w / 2);
        var dy = (y + ph / 2) - (o.y + o.h / 2);
        var minD = (pw + ph) / 4 + timerRadius(o);
        if (dx * dx + dy * dy < minD * minD) { clear = false; break; }
      }
      if (clear) break;
    }
    // Same 30-60 degree launch as the logo, at the configured glide speed.
    var ang = (30 + Math.random() * 30) * Math.PI / 180;
    var b = {
      id: t.id, el: parts.el, timeEl: parts.timeEl, icon: parts.iconChar,
      x: x, y: y, w: pw, h: ph,
      vx: (Math.random() < 0.5 ? -1 : 1) * Math.cos(ang) * SPEED,
      vy: (Math.random() < 0.5 ? -1 : 1) * Math.sin(ang) * SPEED,
      deadline: t.deadline_epoch, done: false, shown: '',
    };
    if (t.expired) markTimerDone(b);
    timerBodies.push(b);
  }

  // Reconcile the simulated pills with a fresh registry list: new timers
  // spawn at a random free spot, dismissed ones despawn, deadlines refresh,
  // and anything past the body cap collapses into the static "+N more" pill.
  function syncTimerBodies(list) {
    if (!overlay) return;
    var vp = layoutSize();
    var shown = list.slice(0, TIMER_CAP);
    var seen = {};
    for (var i = 0; i < shown.length; i++) {
      var t = shown[i];
      seen[t.id] = true;
      var b = null;
      for (var j = 0; j < timerBodies.length; j++) {
        if (timerBodies[j].id === t.id) { b = timerBodies[j]; break; }
      }
      if (!b) { spawnTimerBody(t, vp.w, vp.h); continue; }
      b.deadline = t.deadline_epoch;
      if (t.expired) markTimerDone(b);
    }
    for (var k = timerBodies.length - 1; k >= 0; k--) {
      if (seen[timerBodies[k].id]) continue;
      var el = timerBodies[k].el;
      if (el.parentNode) el.parentNode.removeChild(el);
      timerBodies.splice(k, 1);
    }
    var extra = list.length - shown.length;
    if (extra > 0) {
      if (!timerOverflow) {
        timerOverflow = document.createElement('div');
        timerOverflow.className = 'ss-timer-more';
        timerOverflow.style.cssText =
          'position:absolute;left:50%;bottom:2.5vmin;transform:translateX(-50%);' +
          'z-index:4;padding:0.8vmin 2vmin;border-radius:999px;' +
          'background:rgba(24,26,32,0.85);border:1px solid rgba(255,255,255,0.15);' +
          'color:#9aa0a6;font-size:2.2vmin;';
        overlay.appendChild(timerOverflow);
      }
      timerOverflow.textContent = '+' + extra + ' more';
    } else if (timerOverflow) {
      if (timerOverflow.parentNode) timerOverflow.parentNode.removeChild(timerOverflow);
      timerOverflow = null;
    }
  }

  function pollTimers() {
    // The saver only shows on a visible page; if the browser window is
    // hidden anyway (display power-off), skip the fetch and let the pills
    // keep counting locally from deadline_epoch.
    if (document.hidden) return;
    // Ride the consolidated kiosk poll when it is running (base.html loads it):
    // read the shared timers snapshot instead of a second /timers fetch, so the
    // saver adds no network traffic on top of the one poll. Fall back to a
    // direct fetch on a page without the shared loop (the setup wizard).
    var shared = window.PRKioskStatus && window.PRKioskStatus.last();
    if (shared && shared.timers && Array.isArray(shared.timers.timers)) {
      if (overlay) syncTimerBodies(shared.timers.timers);
      return;
    }
    fetch('timers', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!overlay) return;  // dismissed while fetching
        syncTimerBodies((data && data.timers) || []);
      })
      .catch(function () {
        // Offline poll: keep the pills we have, they count down from their
        // epoch deadlines without the server.
      });
  }

  // Equal-mass elastic collision between two pills: swap the velocity
  // components along the collision normal (only while approaching, so a
  // resolved pair does not re-collide), then split the overlap so nothing
  // sticks.
  function collideTimerPair(a, c) {
    var dx = (c.x + c.w / 2) - (a.x + a.w / 2);
    var dy = (c.y + c.h / 2) - (a.y + a.h / 2);
    var dist = Math.sqrt(dx * dx + dy * dy);
    var minD = timerRadius(a) + timerRadius(c);
    if (dist >= minD) return;
    if (dist < 0.001) { dx = 1; dy = 0; dist = 1; }  // stacked: pick a normal
    var nx = dx / dist, ny = dy / dist;
    var van = a.vx * nx + a.vy * ny;
    var vcn = c.vx * nx + c.vy * ny;
    if (van - vcn > 0) {
      a.vx += (vcn - van) * nx; a.vy += (vcn - van) * ny;
      c.vx += (van - vcn) * nx; c.vy += (van - vcn) * ny;
    }
    var push = (minD - dist) / 2;
    a.x -= nx * push; a.y -= ny * push;
    c.x += nx * push; c.y += ny * push;
  }

  // The logo block is effectively infinite mass: the pill reflects off it and
  // is pushed fully clear; the logo's dead-straight DVD line never bends.
  function collideTimerWithLogo(b, logo) {
    var dx = (b.x + b.w / 2) - logo.cx;
    var dy = (b.y + b.h / 2) - logo.cy;
    var dist = Math.sqrt(dx * dx + dy * dy);
    var minD = timerRadius(b) + logo.r;
    if (dist >= minD) return;
    if (dist < 0.001) { dx = 1; dy = 0; dist = 1; }
    var nx = dx / dist, ny = dy / dist;
    var vn = b.vx * nx + b.vy * ny;
    if (vn < 0) { b.vx -= 2 * vn * nx; b.vy -= 2 * vn * ny; }
    b.x += nx * (minD - dist);
    b.y += ny * (minD - dist);
  }

  // One physics step for the pills: integrate, reflect off the panel walls
  // (layout units, panel only: the deck band is the logo's territory), solve
  // collisions, then render transforms and the local countdowns. In bounce
  // mode this is called from the logo's step; in photos mode from its own
  // small loop. Countdown text between polls is deadline_epoch minus the
  // panel's own clock, the same formula every other surface uses.
  function stepTimerBodies(ts, w, h, logo) {
    if (!timerBodies.length) { timerLast = ts; return; }
    var dt = timerLast === null ? 0 : Math.min(100, ts - timerLast) / 1000;
    timerLast = ts;
    var i, b;
    for (i = 0; i < timerBodies.length; i++) {
      b = timerBodies[i];
      b.w = b.el.offsetWidth || b.w;
      b.h = b.el.offsetHeight || b.h;
      b.x += b.vx * dt;
      b.y += b.vy * dt;
    }
    for (i = 0; i < timerBodies.length; i++) {
      for (var j = i + 1; j < timerBodies.length; j++) {
        collideTimerPair(timerBodies[i], timerBodies[j]);
      }
      if (logo) collideTimerWithLogo(timerBodies[i], logo);
    }
    var nowSec = Date.now() / 1000;
    for (i = 0; i < timerBodies.length; i++) {
      b = timerBodies[i];
      // Walls last, so a collision separation can never leave a pill drawn
      // past the edge: reflect exactly at the wall, like the logo does.
      var maxX = Math.max(0, w - b.w);
      var maxY = Math.max(0, h - b.h);
      if (b.x <= 0) { b.x = 0; b.vx = Math.abs(b.vx); }
      else if (b.x >= maxX) { b.x = maxX; b.vx = -Math.abs(b.vx); }
      if (b.y <= 0) { b.y = 0; b.vy = Math.abs(b.vy); }
      else if (b.y >= maxY) { b.y = maxY; b.vy = -Math.abs(b.vy); }
      b.el.style.transform = 'translate(' + b.x + 'px,' + b.y + 'px)';
      if (b.done) continue;
      var remaining = b.deadline - nowSec;
      if (remaining <= 0) { markTimerDone(b); continue; }
      // Last-minute stage: inside the final 60 seconds the pill's face starts
      // a gentle hop (a CSS animation on the face only, so the physics body
      // keeps its straight line). Extending the timer past a minute again
      // calms it back down.
      b.el.classList.toggle('ss-timer-ending', remaining <= 60);
      var txt = fmtRemaining(remaining);
      if (txt !== b.shown) { b.shown = txt; b.timeEl.textContent = txt; }
    }
  }

  // Photos mode has no rAF of its own, so the pills get a small dedicated
  // loop there; it stands down if the bounce fallback takes over mid-run.
  function startTimerLoop() {
    if (timerRafId) return;
    function tstep(ts) {
      if (!overlay || bounceActive) { timerRafId = null; return; }
      var vp = layoutSize();
      stepTimerBodies(ts, vp.w, vp.h, null);
      timerRafId = requestAnimationFrame(tstep);
    }
    timerRafId = requestAnimationFrame(tstep);
  }

  // Test-only view of the simulated pills (layout px, px/s), sampled by the
  // automated physics probe to check walls and collisions.
  window.__screensaverTimers = function () {
    return timerBodies.map(function (b) {
      return { id: b.id, x: b.x, y: b.y, vx: b.vx, vy: b.vy,
               w: b.w, h: b.h, done: b.done };
    });
  };

  // Build the bouncing logo+clock block inside the overlay (the default
  // style, and the fallback when the photo list is empty or unreachable).
  function startBounceMode() {
    if (!overlay) return;
    var block = document.createElement('div');
    block.className = 'ss-block';
    block.style.cssText =
      'position:absolute;left:0;top:0;text-align:center;color:#9aa0a6;' +
      'font-family:inherit;will-change:transform;';
    var mark = document.createElement('img');
    mark.src = 'static/icons/logo-mark.png';
    mark.alt = '';
    mark.style.cssText = 'width:18vmin;height:18vmin;opacity:0.85;display:block;margin:0 auto 1.5vmin;';
    var time = document.createElement('div');
    time.className = 'ss-time';
    time.style.cssText = 'font-size:6vmin;font-weight:600;line-height:1;color:#cfd3d8;';
    var date = document.createElement('div');
    date.className = 'ss-date';
    date.style.cssText = 'font-size:2.4vmin;margin-top:0.8vmin;opacity:0.7;';
    block.appendChild(mark);
    block.appendChild(time);
    block.appendChild(date);
    overlay.appendChild(block);
    updateClock();
    startBounce(block);
  }

  // The slideshow src list from a /ui/screensaver/photos response: `urls`
  // entries are ready-to-use (folder, Immich, and direct-link sources send
  // these), while bare `photos` names are the legacy USB shape and become
  // the old per-name proxy URL. Pure, exposed for the tests.
  function photoSrcList(data) {
    var urls = data && data.urls;
    if (Array.isArray(urls)) {
      urls = urls.filter(function (u) { return typeof u === 'string' && u; });
      if (urls.length) return urls;
    }
    var names = (data && data.photos) || [];
    if (!Array.isArray(names)) return [];
    return names.filter(function (n) { return typeof n === 'string' && n; })
      .map(function (n) {
        return 'ui/screensaver/photo?name=' + encodeURIComponent(n);
      });
  }
  window.__photoSrcList = photoSrcList;

  // Photo slideshow. Each image is cover-fit and drifts with a slow Ken Burns
  // pan/zoom (a long CSS transform transition, so the compositor does the
  // work); the next image crossfades in on its own layer. Order is shuffled
  // per saver run, reshuffled when the deck runs out. EXIF rotation is the
  // browser's job (image-orientation: from-image). The corner clock moves to
  // a different corner with every photo as the burn-in guard.
  function startPhotosMode(srcs) {
    if (!overlay) return;
    var order = srcs.slice();
    for (var i = order.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = order[i]; order[i] = order[j]; order[j] = t;
    }
    var idx = 0;
    var current = null;
    var corners = ['right:5vmin;bottom:3vmin;', 'left:5vmin;bottom:3vmin;',
                   'left:5vmin;top:3vmin;', 'right:5vmin;top:3vmin;'];
    var cornerIdx = 0;

    var clock = document.createElement('div');
    clock.className = 'ss-time';
    clock.style.cssText = cornerClockCss(corners[0]);
    overlay.appendChild(clock);
    updateClock();
    startTimerLoop();  // photos have no rAF of their own; the pills need one

    function kenBurns(img) {
      if (!KEN_BURNS || REDUCED_MOTION) {
        // Held still: cover-fit with only the crossfade, no pan or zoom.
        // Also the path a reduced-motion request takes.
        img.style.transition = 'opacity ' + FADE_MS + 'ms ease';
        return;
      }
      // Speed picks how far the frame travels (kenBurnsFrame keeps it covered
      // at every moment). Random start and end offsets within the pan budget
      // give each photo its own direction; the scale grows from start to end
      // so the zoom is felt too. Linear so the drift never visibly stops.
      var f = kenBurnsFrame(KEN_BURNS_SPEED);
      function off() {
        return ((Math.random() * 2 - 1) * f.pan).toFixed(2) + '%';
      }
      img.style.transform =
        'scale(' + f.startScale.toFixed(3) + ') translate(' + off() + ',' + off() + ')';
      img.getBoundingClientRect(); // commit the start frame
      img.style.transition = 'opacity ' + FADE_MS + 'ms ease, transform ' +
        (PHOTO_MS + FADE_MS * 2) + 'ms linear';
      img.style.transform =
        'scale(' + f.endScale.toFixed(3) + ') translate(' + off() + ',' + off() + ')';
    }

    function advance() {
      if (!overlay) return;
      var src = order[idx];
      idx += 1;
      if (idx >= order.length) {
        idx = 0;
        order.sort(function () { return Math.random() - 0.5; });
      }
      var img = document.createElement('img');
      img.alt = '';
      img.style.cssText =
        'position:absolute;inset:0;width:100%;height:100%;object-fit:cover;' +
        'image-orientation:from-image;opacity:0;will-change:transform,opacity;';
      img.onload = function () {
        if (!overlay || img.parentNode !== overlay) return;
        kenBurns(img);
        img.style.opacity = '1';
        var old = current;
        current = img;
        cornerIdx = (cornerIdx + 1) % corners.length;
        clock.style.cssText = cornerClockCss(corners[cornerIdx]);
        if (old) {
          old.style.opacity = '0';
          setTimeout(function () {
            if (old.parentNode) old.parentNode.removeChild(old);
          }, FADE_MS + 200);
        }
        photoTimer = setTimeout(advance, PHOTO_MS);
      };
      img.onerror = function () {
        // A vanished file (drive pulled mid-show, a dead link) skips ahead.
        if (img.parentNode) img.parentNode.removeChild(img);
        photoTimer = setTimeout(advance, 1000);
      };
      img.src = src;
      overlay.insertBefore(img, clock);
    }
    advance();
  }

  // -- retro canvas savers: flying toasters + starfield (FoodAssistant-umnk) --
  //
  // Both draw on a single canvas under one requestAnimationFrame loop that also
  // steps the timer pills (stepTimerBodies, logo=null), so the pills ride these
  // modes exactly like they ride photos, and there is never a second loop. Every
  // sprite is drawn programmatically, so nothing loads from the network. Sprite
  // counts are capped for a Pi 3 and thinned for a reduced-motion request; no
  // per-frame string or object allocation happens in the draw path. A draw error
  // tears the canvas down and falls back to the bouncing logo, so a broken mode
  // is never a frozen black screen.

  // Sprite budget for a canvas mode, capped small for a Pi 3 and thinned for a
  // reduced-motion request. Pure, so the tests can check the caps.
  function screensaverSpriteBudget(kind, reduced) {
    if (kind === 'starfield') return reduced ? 40 : 110;
    return reduced ? 7 : 12;   // toasters, spread across three parallax layers
  }
  window.__screensaverSpriteBudget = screensaverSpriteBudget;

  // A diagonal sprite has flown clear once its whole box is past the left edge
  // OR below the bottom edge (the classic upper-right to lower-left drift), and
  // is due to respawn. Pure, tested through node.
  function spriteOffscreen(x, y, size, w, h) {
    return (x + size < 0) || (y - size > h);
  }
  window.__spriteOffscreen = spriteOffscreen;

  // Rounded-rectangle path (Pi kiosk Chromium predates ctx.roundRect).
  function rrect(ctx, x, y, w, h, r) {
    var rr = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  // One feathered wing in unit coordinates, splaying toward -x (the caller
  // mirrors it with scale(-1,1) for the other side). Drawn from literals so
  // nothing is allocated per frame. Shared by both of a toaster's wings.
  function drawToasterWing(ctx, fill, line) {
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.quadraticCurveTo(-1.2, -0.55, -1.55, 0.05);
    ctx.quadraticCurveTo(-1.1, 0.22, 0, 0.3);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = line;
    ctx.lineWidth = 0.05;
    ctx.beginPath();
    ctx.moveTo(-0.22, 0.05); ctx.lineTo(-1.0, -0.18);
    ctx.moveTo(-0.22, 0.15); ctx.lineTo(-1.0, 0.02);
    ctx.moveTo(-0.22, 0.24); ctx.lineTo(-0.85, 0.16);
    ctx.stroke();
  }

  // Draw a winged toaster in unit coordinates (the caller scales it). The body
  // is a chrome box seen at a slight 3/4 perspective so it reads as a solid
  // metallic object, not a flat silhouette (FoodAssistant-umnk on-device
  // review): a lit front face with flat chrome highlight/shadow bands (flat
  // bands, never a per-frame canvas gradient, to stay Pi-3 cheap), a receding
  // darker right side face, and a top face carrying the two slots with slices
  // of toast rising out. TWO feathered wings, one on each side, flap in sync
  // through Math.sin(wing) (mirrored left/right). pink swaps the chrome for
  // brand pink, the Pantry Raider wink on the After Dark tribute. Every colour
  // is a literal, so nothing is allocated per frame.
  function drawToaster(ctx, wing, pink) {
    var front = pink ? '#f2006e' : '#c7ccd4';
    var hi = pink ? '#ff7ab3' : '#eef1f5';
    var lo = pink ? '#a80050' : '#8f949c';
    var side = pink ? '#7a0038' : '#6f757e';   // receding face, in shadow
    var top = pink ? '#ff2e86' : '#dfe3e8';    // top face catching the light
    var wingFill = pink ? '#ffd1e6' : '#f7f9fc';
    var wingLine = pink ? '#e58bb5' : '#c9d2dc';
    // Perspective skew: the far top-right of the box recedes by (dx, dy).
    var dx = 0.34, dy = -0.30;
    // Both wings first, behind the body, so they splay out from the shoulders.
    // Same `wing` drives both; the right one is mirrored, so they flap in sync.
    ctx.save();
    ctx.translate(-0.9, 0.0);
    ctx.rotate(wing);
    drawToasterWing(ctx, wingFill, wingLine);
    ctx.restore();
    ctx.save();
    ctx.translate(0.72, 0.0);
    ctx.scale(-1, 1);
    ctx.rotate(wing);
    drawToasterWing(ctx, wingFill, wingLine);
    ctx.restore();
    // Two slices of toast rising from the slots, behind the body so only the
    // tips above the top face show.
    ctx.fillStyle = '#d9a25a';
    rrect(ctx, -0.46 + dx * 0.5, -1.05, 0.46, 0.62, 0.1); ctx.fill();
    rrect(ctx, 0.06 + dx * 0.5, -1.05, 0.46, 0.62, 0.1); ctx.fill();
    ctx.fillStyle = '#b9793a';
    rrect(ctx, -0.46 + dx * 0.5, -1.05, 0.46, 0.14, 0.08); ctx.fill();
    rrect(ctx, 0.06 + dx * 0.5, -1.05, 0.46, 0.14, 0.08); ctx.fill();
    // Receding right side face (darkest), drawn first so the front overlaps it.
    ctx.fillStyle = side;
    ctx.beginPath();
    ctx.moveTo(0.7, -0.30);
    ctx.lineTo(0.7 + dx, -0.30 + dy);
    ctx.lineTo(0.7 + dx, 0.70 + dy);
    ctx.lineTo(0.7, 0.70);
    ctx.closePath();
    ctx.fill();
    // Top face (bright), a parallelogram receding up and to the right.
    ctx.fillStyle = top;
    ctx.beginPath();
    ctx.moveTo(-1.0, -0.30);
    ctx.lineTo(0.7, -0.30);
    ctx.lineTo(0.7 + dx, -0.30 + dy);
    ctx.lineTo(-1.0 + dx, -0.30 + dy);
    ctx.closePath();
    ctx.fill();
    // Slots on the top face, with a sliver of toast showing in each.
    ctx.fillStyle = '#2a2d33';
    rrect(ctx, -0.52 + dx * 0.5, -0.52, 0.46, 0.12, 0.05); ctx.fill();
    rrect(ctx, 0.06 + dx * 0.5, -0.52, 0.46, 0.12, 0.05); ctx.fill();
    ctx.fillStyle = '#d9a25a';
    rrect(ctx, -0.48 + dx * 0.5, -0.55, 0.38, 0.06, 0.03); ctx.fill();
    rrect(ctx, 0.10 + dx * 0.5, -0.55, 0.38, 0.06, 0.03); ctx.fill();
    // Lit front face (chrome), with flat highlight and shadow bands for the
    // dimensional shine instead of a per-frame gradient.
    ctx.fillStyle = front;
    rrect(ctx, -1.0, -0.30, 1.7, 1.0, 0.22); ctx.fill();
    ctx.fillStyle = hi;
    rrect(ctx, -0.92, -0.22, 1.54, 0.26, 0.13); ctx.fill();
    ctx.fillStyle = lo;
    rrect(ctx, -0.92, 0.48, 1.54, 0.16, 0.1); ctx.fill();
    // Lever on the receding side face.
    ctx.fillStyle = lo;
    rrect(ctx, 0.66 + dx, 0.02, 0.14, 0.32, 0.06); ctx.fill();
  }

  // A plain flying slice of toast (some sprites are just toast, like the
  // original saver): crust edge and a pat of butter.
  function drawToast(ctx) {
    ctx.fillStyle = '#d9a25a';
    rrect(ctx, -0.7, -0.6, 1.4, 1.2, 0.3); ctx.fill();
    ctx.fillStyle = '#b9793a';
    rrect(ctx, -0.7, -0.6, 1.4, 0.26, 0.24); ctx.fill();
    ctx.fillStyle = 'rgba(255,225,130,0.85)';
    rrect(ctx, -0.18, -0.12, 0.36, 0.3, 0.06); ctx.fill();
  }

  // Send a toaster/toast sprite back to a fresh spot. The first spawn spreads
  // sprites across the whole screen so the field is full immediately; later
  // respawns enter just off the top or right edge to feed the down-left stream.
  function respawnToaster(sp, w, h, initial) {
    var vmin = Math.min(w, h);
    sp.size = vmin * sp.sizef;
    sp.spawned = true;
    if (initial) {
      sp.x = Math.random() * w;
      sp.y = Math.random() * h;
      return;
    }
    if (Math.random() < 0.5) {
      sp.x = Math.random() * (w + sp.size * 2) - sp.size;
      sp.y = -sp.size - Math.random() * sp.size * 4;
    } else {
      sp.x = w + sp.size + Math.random() * sp.size * 4;
      sp.y = Math.random() * h * 0.8;
    }
  }

  function drawToasters(ctx, sprites, w, h, dt) {
    for (var i = 0; i < sprites.length; i++) {
      var sp = sprites[i];
      if (!sp.spawned) { respawnToaster(sp, w, h, true); continue; }
      if (spriteOffscreen(sp.x, sp.y, sp.size, w, h)) {
        respawnToaster(sp, w, h, false);
        continue;
      }
      sp.x += sp.vx * dt;
      sp.y += sp.vy * dt;
      sp.wing += sp.wingSpeed * dt;
      ctx.save();
      ctx.translate(sp.x, sp.y);
      ctx.scale(sp.size, sp.size);
      if (sp.toast) {
        sp.rot += sp.rotSpeed * dt;
        ctx.rotate(sp.rot);
        drawToast(ctx);
      } else {
        drawToaster(ctx, Math.sin(sp.wing) * 0.55 - 0.15, sp.pink);
      }
      ctx.restore();
    }
  }

  // Warp starfield: each star streaks outward from the centre, accelerating as
  // its radius grows, and respawns at the centre on a new angle when it passes
  // the corner. Brightness and streak width grow with radius. globalAlpha (a
  // number) carries per-star brightness so the colour stays a constant literal,
  // keeping the draw path free of per-frame string allocation.
  function drawStarfield(ctx, stars, w, h, dt, k) {
    var cx = w / 2, cy = h / 2;
    var maxR = Math.sqrt(cx * cx + cy * cy) || 1;
    for (var i = 0; i < stars.length; i++) {
      var st = stars[i];
      if (!st.init) { st.r = Math.random() * maxR * 0.6 + 4; st.init = true; }
      var pr = st.r;
      st.r += (st.r * 0.9 + 30) * st.speed * k * dt;
      if (st.r >= maxR) { st.ang = Math.random() * Math.PI * 2; st.r = 4; pr = 4; }
      var ca = Math.cos(st.ang), sa = Math.sin(st.ang);
      var b = st.r / maxR;
      ctx.strokeStyle = st.pink ? '#f2006e' : '#ebf0ff';
      ctx.globalAlpha = 0.35 + b * 0.65;
      ctx.lineWidth = 0.6 + b * 2.4;
      ctx.beginPath();
      ctx.moveTo(cx + ca * pr, cy + sa * pr);
      ctx.lineTo(cx + ca * st.r, cy + sa * st.r);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // Build and run a canvas saver (kind: 'toasters' | 'starfield'). One rAF loop
  // clears to black, draws the field, then steps the timer pills. A hopping
  // corner clock is the burn-in guard. Any draw error falls back to bounce.
  function startCanvasMode(kind) {
    if (!overlay) return;
    var canvas = document.createElement('canvas');
    canvas.style.cssText =
      'position:absolute;inset:0;width:100%;height:100%;z-index:1;';
    overlay.appendChild(canvas);
    var ctx = null;
    try { ctx = canvas.getContext('2d'); } catch (e) { ctx = null; }
    if (!ctx) { if (canvas.parentNode) canvas.remove(); startBounceMode(); return; }

    var corners = ['right:6vmin;bottom:4vmin;', 'left:6vmin;bottom:4vmin;',
                   'left:6vmin;top:4vmin;', 'right:6vmin;top:4vmin;'];
    var cornerIdx = 0;
    var clock = document.createElement('div');
    clock.className = 'ss-time';
    function placeClock() {
      clock.style.cssText = cornerClockCss(corners[cornerIdx]);
    }
    placeClock();
    overlay.appendChild(clock);
    updateClock();
    var clockHop = setInterval(function () {
      if (!overlay) return;
      cornerIdx = (cornerIdx + 1) % corners.length;
      placeClock();
    }, 20000);
    canvasCleanup = function () { clearInterval(clockHop); };

    var reduced = REDUCED_MOTION;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);  // cap fill rate
    var cw = 0, ch = 0;
    function ensureSize(w, h) {
      var bw = Math.round(w * dpr), bh = Math.round(h * dpr);
      if (bw === cw && bh === ch) return;
      cw = bw; ch = bh;
      canvas.width = bw; canvas.height = bh;
    }

    var count = screensaverSpriteBudget(kind, reduced);
    var sprites = null, stars = null;
    if (kind === 'toasters') {
      sprites = [];
      var sizeF = [0.055, 0.085, 0.12];
      var speedF = [0.55, 0.8, 1.15];
      var TSPEED = SPEED * 3.5 * (reduced ? 0.55 : 1);
      var ta = 33 * Math.PI / 180;
      for (var i = 0; i < count; i++) {
        var layer = i % 3;
        var isToast = (i % 4 === 3);
        var speed = TSPEED * speedF[layer];
        sprites.push({
          toast: isToast, sizef: sizeF[layer], size: 1, spawned: false,
          x: 0, y: 0,
          vx: -Math.cos(ta) * speed, vy: Math.sin(ta) * speed,
          wing: Math.random() * Math.PI * 2, wingSpeed: 6 + Math.random() * 3,
          rot: 0, rotSpeed: (Math.random() - 0.5) * 1.5,
          pink: !isToast && (i % 6 === 0),
        });
      }
    } else {
      stars = [];
      for (var s = 0; s < count; s++) {
        stars.push({
          ang: Math.random() * Math.PI * 2, r: 0,
          speed: 0.35 + Math.random() * 0.65,
          pink: (s % 14 === 0), init: false,
        });
      }
    }
    var STAR_K = reduced ? 0.55 : 1;

    var last = null;
    function frame(ts) {
      if (!overlay) return;
      try {
        var vp = layoutSize();
        var w = vp.w, h = vp.h;
        ensureSize(w, h);
        var dt = last === null ? 0 : Math.min(100, ts - last) / 1000;
        last = ts;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, w, h);
        if (kind === 'toasters') drawToasters(ctx, sprites, w, h, dt);
        else drawStarfield(ctx, stars, w, h, dt, STAR_K);
        stepTimerBodies(ts, w, h, null);
      } catch (e) {
        if (canvasCleanup) { canvasCleanup(); canvasCleanup = null; }
        if (canvas.parentNode) canvas.remove();
        if (clock.parentNode) clock.remove();
        rafId = null;
        if (overlay) startBounceMode();
        return;
      }
      rafId = requestAnimationFrame(frame);
    }
    rafId = requestAnimationFrame(frame);
  }

  // Tell the host bridge whether the soft screensaver overlay is showing, so
  // the Stream Deck can raise its display-off logo while the kitchen screen
  // sleeps under the overlay even though the panel itself is still lit
  // (FoodAssistant-qh8p). Only the real kiosk panel reports: a saver running in
  // some other browser (all-clients mode) must never put the deck to sleep.
  // Edge-triggered from show()/hide() only, never a repeating poll, and it
  // POSTs to a dedicated screensaver endpoint that does NOT bump the shared
  // activity epoch, so it adds no new wake source (FoodAssistant-ofip).
  function reportScreensaverState(active) {
    if (!kiosk) return;
    try {
      fetch('setup/kiosk/screensaver', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: !!active }),
        cache: 'no-store',
      }).catch(function () { /* no bridge / not a Pi: nothing to report */ });
    } catch (e) { /* fetch unavailable: ignore */ }
  }

  function show() {
    if (overlay) return;
    // Hide the pointer everywhere while the saver is up, not just over the
    // overlay: Chromium only refreshes the cursor shape on movement, and a
    // cursor parked before the overlay appeared would otherwise stay drawn.
    var style = document.createElement('style');
    style.id = 'kiosk-screensaver-cursor';
    style.textContent =
      'body.ss-active, body.ss-active * { cursor: none !important; }' +
      // Finished-timer alarm: the pill pulses between red and amber with a
      // matching glow, unmistakable from across the kitchen.
      '@keyframes ss-timer-pulse{' +
      '0%,100%{background:#a4231b;box-shadow:0 0 3vmin rgba(255,80,40,0.8);}' +
      '50%{background:#b36a00;box-shadow:0 0 5.5vmin rgba(255,170,0,0.9);}}' +
      // Last minute of a countdown: a gentle vertical hop. Both stage
      // animations run on the pill's face, never its physics shell, so the
      // drift and collisions stay deterministic.
      // Last minute: a pulsing brand-pink glow breathes from behind the
      // pill (the earlier vertical hop read as swimming; a glow warns just
      // as clearly without fighting the drift motion).
      '@keyframes ss-timer-glow{' +
      '0%,100%{box-shadow:0 0 0 0 rgba(242,0,110,0.0), 0 0 1.2vmin 0.2vmin rgba(242,0,110,0.35);}' +
      '50%{box-shadow:0 0 0 0.45vmin rgba(242,0,110,0.55), 0 0 3vmin 1vmin rgba(242,0,110,0.75);}}' +
      // Finished: the whole pill turns slowly, 5 seconds per revolution, slow
      // enough that the food icon and Done stay readable mid-spin.
      '@keyframes ss-timer-spin{' +
      'from{transform:rotate(0deg);}to{transform:rotate(360deg);}}' +
      '#kiosk-screensaver .ss-timer-ending .ss-timer-face{' +
      'animation:ss-timer-glow 1.4s ease-in-out infinite;' +
      'border-color:rgba(242,0,110,0.8);}' +
      '#kiosk-screensaver .ss-timer-done .ss-timer-face{' +
      'animation:ss-timer-pulse 1.1s ease-in-out infinite,' +
      'ss-timer-spin 5s linear infinite;' +
      'border-color:rgba(255,200,120,0.85);color:#fff;}';
    document.head.appendChild(style);
    document.body.classList.add('ss-active');
    overlay = document.createElement('div');
    overlay.id = 'kiosk-screensaver';
    overlay.style.cssText =
      'position:fixed;inset:0;width:100%;height:100%;max-width:100%;' +
      'max-height:100%;z-index:2147483000;background:#000;' +
      'opacity:0;transition:opacity 1.2s ease;cursor:none;overflow:hidden;';
    document.body.appendChild(overlay);
    // Fade in on the next frame so the transition runs.
    requestAnimationFrame(function () {
      if (overlay) overlay.style.opacity = '1';
    });
    // Edge-triggered: the saver just went up, so tell the bridge (kiosk only).
    reportScreensaverState(true);
    clockTimer = setInterval(updateClock, 5000);
    // Active timers ride the saver as bouncing pills: poll the shared
    // registry only while the saver is showing, starting right away so the
    // pills appear with the fade-in.
    pollTimers();
    timerPollId = setInterval(pollTimers, TIMER_POLL_MS);
    if (MODE === 'photos') {
      // The list is fetched fresh at every saver start so plugging in a
      // drive, dropping a photo in the folder, or updating the Immich album
      // takes effect on the next idle, no restart needed. The server sends
      // ready-to-use src strings in `urls` from whichever photo source is
      // configured (FoodAssistant-af1l); `photos` bare names are the legacy
      // shape, kept as the fallback. Any failure or an empty list falls back
      // to the bouncing logo, so the setting is always safe to leave on.
      fetch('ui/screensaver/photos', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!overlay) return; // dismissed while fetching
          var list = photoSrcList(data);
          if (list.length) startPhotosMode(list);
          else startBounceMode();
        })
        .catch(function () { if (overlay) startBounceMode(); });
    } else if (MODE === 'toasters' || MODE === 'starfield') {
      startCanvasMode(MODE);
    } else {
      startBounceMode();
    }
  }

  function hide() {
    if (!overlay) return;
    var el = overlay;
    overlay = null;
    clearInterval(clockTimer);
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    if (photoTimer) clearTimeout(photoTimer);
    photoTimer = null;
    if (canvasCleanup) { canvasCleanup(); canvasCleanup = null; }
    clearInterval(timerPollId);
    timerPollId = null;
    if (timerRafId) cancelAnimationFrame(timerRafId);
    timerRafId = null;
    timerLast = null;
    timerBodies = [];       // the pill elements go down with the overlay
    timerOverflow = null;
    bounceActive = false;
    if (el.parentNode) el.parentNode.removeChild(el);
    document.body.classList.remove('ss-active');
    var style = document.getElementById('kiosk-screensaver-cursor');
    if (style && style.parentNode) style.parentNode.removeChild(style);
    // Edge-triggered: the saver just came down, so the screen is awake again.
    reportScreensaverState(false);
    ssManualOpen = false;   // a fresh manual open re-arms its own suppression
  }

  // After a dismissing tap, keep swallowing the rest of its gesture (the
  // pointerup/touchend/click that follow) so the tap never presses whatever
  // sits under the overlay.
  var suppressUntil = 0;
  var SWALLOW = ['pointerdown', 'pointerup', 'touchstart', 'touchend',
                 'mousedown', 'mouseup', 'click', 'keydown'];

  // A test start (the settings page button) briefly ignores mouse motion so
  // the pointer drifting off the button does not kill the preview instantly;
  // a deliberate touch, click, or key press still dismisses right away.
  var motionGraceUntil = 0;

  function onActivity(ev) {
    lastActivity = Date.now();
    var swallow = ev && SWALLOW.indexOf(ev.type) !== -1;
    if (overlay) {
      if (!swallow && Date.now() < motionGraceUntil) return;
      // Any input wakes the screen; a tap/key that did it is swallowed so it
      // only dismisses the screensaver. Mouse motion just dismisses.
      if (swallow) {
        suppressUntil = Date.now() + 700;
        if (ev.cancelable) ev.preventDefault();
        ev.stopPropagation();
      }
      // Waking the screensaver is activity, and stopPropagation above means
      // kiosk-idle.js never sees the tap that did it. Without this the panel
      // kept counting toward display sleep from before the screensaver came
      // up, so a touched-awake screen went black moments later
      // (FoodAssistant-9k2v). Looked up at call time: script order is not
      // guaranteed, and the hook is absent off a kiosk, where there is no
      // bridge to tell anyway.
      if (typeof window.__prKioskActivity === 'function') {
        window.__prKioskActivity();
      }
      hide();
      return;
    }
    if (swallow && Date.now() < suppressUntil) {
      if (ev.cancelable) ev.preventDefault();
      ev.stopPropagation();
    }
  }

  var events = SWALLOW.concat(['mousemove', 'wheel']);
  for (var i = 0; i < events.length; i++) {
    // Capture phase so a dismissing tap is seen (and swallowed) before the
    // page's own handlers. passive:false lets preventDefault work on touch.
    window.addEventListener(events[i], onActivity, { capture: true, passive: false });
  }

  // Watching a camera is intentionally idle (FoodAssistant-ysf6): never let
  // the saver cover the camera page or an ha-events camera pop-up. Refreshing
  // lastActivity while one is up means the full idle countdown restarts when
  // it goes away.
  function cameraOnScreen() {
    if (/(^|\/)ui\/camera(\/|$)/.test(window.location.pathname)) return true;
    if (document.querySelector('.hae-cam')) return true;
    return false;
  }

  // Idle activation runs on a kiosk with a timeout configured, or in every
  // browser when the all-clients setting is on; the test hook below works
  // everywhere the script loads either way.
  if ((kiosk || ALL_CLIENTS) && IDLE_MS > 0) {
    setInterval(function () {
      if (overlay) return;
      if (cameraOnScreen()) { lastActivity = Date.now(); return; }
      if (Date.now() - lastActivity >= IDLE_MS) show();
    }, 10000);
  }

  // -- cross-surface wake (FoodAssistant-fho8) ------------------------------
  //
  // A Stream Deck press wakes the physical panel (the deck reports activity to
  // the host bridge, which powers the display back on), but it never touches
  // this kiosk browser, so a deck press alone would leave the screensaver
  // overlay covering the woken screen. This is the kiosk half of the deck's
  // own cross-surface wake: poll the bridge's shared last-activity epoch
  // (relayed by the app at setup/kiosk/activity) and, when another surface was
  // just active, treat it as local activity so the saver does not arm and
  // dismiss it if it is already up. Polling is read-only, so it never bumps the
  // activity epoch itself (no spurious deck wakes, FoodAssistant-ofip).
  var EXTERNAL_ACTIVITY_WINDOW_SECS = 12;
  var EXTERNAL_POLL_MS = 4000;

  // Mirror of the deck's controller._external_wake_due. Given the bridge's
  // shared last-activity epoch, decide whether another surface (a deck press)
  // was active recently enough to wake this kiosk, and what to remember for the
  // next poll. Two signals count, so a slow poll tick cannot miss a press: the
  // report is inside the freshness window, or the epoch ADVANCED past the one
  // seen last time. The first poll (prevSeen null) trusts only the window, so a
  // stale epoch from before the page loaded never fires a wake; a malformed or
  // future-stamped epoch is ignored. Pure, so the tests run it straight through
  // node.
  function externalWakeDue(lastActivity, nowEpoch, prevSeen, windowSecs) {
    var w = (typeof windowSecs === 'number') ? windowSecs : EXTERNAL_ACTIVITY_WINDOW_SECS;
    if (typeof lastActivity !== 'number' || !isFinite(lastActivity) ||
        lastActivity <= 0 || lastActivity > nowEpoch) {
      return { wake: false, seen: prevSeen };
    }
    var age = nowEpoch - lastActivity;
    var fresh = age >= 0 && age <= w;
    var advanced = (prevSeen !== null && prevSeen !== undefined) &&
      lastActivity > prevSeen;
    return { wake: (fresh || advanced), seen: lastActivity };
  }
  window.__externalWakeDue = externalWakeDue;

  // Grace after a MANUAL open (the Test button, the timers-menu start, a
  // screensaver launch key): a manual open happens right after the user just
  // interacted, so the bridge's last_activity is still fresh (within the
  // window). Without this, the first external-wake poll would read that stale
  // stamp as activity and dismiss the just-opened saver a few seconds later
  // (FoodAssistant-qh8p follow-up). The grace covers the freshness window with
  // a small margin, and `ssSeedExternal` makes the first poll adopt the current
  // activity as the baseline (never dismissing on it) so only NEW activity
  // after the open counts. A genuine touch or deck press AFTER it opens still
  // dismisses immediately (fho8 preserved).
  var ssSeedExternal = false;
  // A manually-opened saver (the settings Test button or the timers-screen
  // button) must not be torn down by the still-fresh activity from the tap that
  // opened it. Unlike an idle-activated saver (where activity is already stale
  // by the time it arms), a manual open has to ignore that freshness for its
  // WHOLE life, not just a few-second grace window: once the grace expired the
  // stale-but-still-fresh stamp dismissed it (FoodAssistant-qh8p follow-up).
  // ssManualOpen holds from the open until hide(); while set, only activity that
  // advances past the open baseline (a real deck press or screen touch) counts.
  var ssManualOpen = false;

  // externalWakeDue narrowed for a manually-opened saver. While it is manually
  // open, mere freshness must NOT dismiss (that is the stale stamp from the tap
  // that opened it); only a strict advance past the open baseline (activity that
  // happened after the open) counts. For an idle-activated saver it is exactly
  // externalWakeDue. Pure, tested through node.
  function screensaverExternalWake(lastActivity, nowEpoch, prevSeen, inGrace,
                                   windowSecs) {
    var res = externalWakeDue(lastActivity, nowEpoch, prevSeen, windowSecs);
    if (inGrace && res.wake) {
      var advanced = (prevSeen !== null && prevSeen !== undefined) &&
        typeof lastActivity === 'number' && lastActivity > prevSeen;
      return { wake: advanced, seen: res.seen };
    }
    return res;
  }
  window.__screensaverExternalWake = screensaverExternalWake;

  if (kiosk || ALL_CLIENTS) {
    var seenExternal = null;
    function processExternal(d) {
      if (!d) return;
      var la = typeof d.last_activity === 'number' ? d.last_activity : 0;
      if (ssSeedExternal) {
        // A manual open just happened: adopt the current activity as the
        // baseline so only activity AFTER the open can dismiss, and never
        // dismiss on this seeding poll.
        if (la > 0) seenExternal = la;
        ssSeedExternal = false;
        return;
      }
      var res = screensaverExternalWake(
        la, Date.now() / 1000, seenExternal, ssManualOpen);
      seenExternal = res.seen;
      if (res.wake) {
        // Another surface (a deck press) is active: count it as local
        // activity so the saver does not arm, and dismiss it if it is up.
        lastActivity = Date.now();
        if (overlay) hide();
      }
    }
    var pollExternalActivity = function () {
      // Ride the consolidated kiosk poll's activity slice (same host-bridge
      // shape) when it is running, instead of a second /setup/kiosk/activity
      // fetch; fall back to a direct fetch without the shared loop.
      var shared = window.PRKioskStatus && window.PRKioskStatus.last();
      if (shared) { processExternal(shared.activity || null); return; }
      fetch('setup/kiosk/activity', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(processExternal)
        .catch(function () { /* no bridge / not a Pi: nothing to adopt */ });
    };
    setInterval(pollExternalActivity, EXTERNAL_POLL_MS);
  }

  // Settings page Test button (FoodAssistant-fiwc): force the saver on now,
  // optionally previewing a speed/style straight from the form so choices can
  // be checked before (or after) saving. The override sticks for this page
  // load only; a reload re-reads the saved settings.
  window.__screensaverTest = function (opts) {
    opts = opts || {};
    if (opts.speed && SPEEDS[opts.speed]) SPEED = SPEEDS[opts.speed];
    // Preview the Ken Burns pan/zoom speed straight from the form too.
    if (opts.kenBurnsSpeed && KEN_BURNS_MAG[opts.kenBurnsSpeed]) {
      KEN_BURNS_SPEED = opts.kenBurnsSpeed;
    }
    // Exact pixels-per-second override, used by the automated wall probe so a
    // test run can cross the screen in a couple of seconds.
    if (opts.speedPx > 0) SPEED = opts.speedPx;
    if (opts.mode) MODE = normalizeMode(opts.mode);
    motionGraceUntil = Date.now() + 1500;
    // Manual open: seed the external-wake baseline on the next poll and mark the
    // saver manually-opened for its whole life, so the still-fresh activity from
    // the tap that opened it never dismisses it (FoodAssistant-qh8p follow-up).
    // Only activity after the open (a real deck press or screen touch) or a
    // local tap dismisses it.
    ssSeedExternal = true;
    ssManualOpen = true;
    show();
  };
})();
