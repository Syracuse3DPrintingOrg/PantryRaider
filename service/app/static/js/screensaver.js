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
// Two styles, chosen by the per-device screensaver_mode setting:
//   bounce  the Pantry Raider logo gliding around the screen with the clock
//           riding under it (the default; constant motion is the burn-in guard)
//   photos  a slideshow of images from an attached USB drive's photos folder,
//           cover-fit with a slow Ken Burns drift and a crossfade every
//           25 seconds; a small clock hops corners between photos so nothing
//           sits still. No drive, no photos, or a failed fetch falls back to
//           the bounce style, so the setting is always safe to leave on.
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
// Shared canvas with the Stream Deck (FoodAssistant-3fdq): when the deck's
// screensaver position setting says the deck sits above/below/left/right of
// the panel, the bounce walls extend past that edge by a band sized from the
// deck's key grid, and the logo's position is posted to ui/screensaver/state
// a few times a second so the deck controller can render the slice crossing
// its keys. The kiosk stays the animation driver; the deck is a slower echo.
// The state replies also carry a dismiss flag, so a deck key press wakes the
// panel's saver too.
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
//
// A FINISHED pill crosses onto the Stream Deck (FoodAssistant-07ee): with the
// deck in the canvas, a done pill's walls extend into the deck band exactly
// like the logo's, so it physically drifts across the seam, and the state
// posts carry the done pills' boxes (same panel-normalized space as the mark)
// so the deck renders its slice of them. Running pills keep panel-only walls;
// only finished timers earn the extra surface.
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
  var MODE = cfg.getAttribute('data-mode') === 'photos' ? 'photos' : 'bounce';
  // Stream Deck canvas position (off disables the shared canvas) and the
  // deck's key-grid height/width ratio, used to size the off-screen band.
  var DECK_LAYOUT = cfg.getAttribute('data-deck-layout') || 'off';
  if (['above', 'below', 'left', 'right'].indexOf(DECK_LAYOUT) === -1) DECK_LAYOUT = 'off';
  var DECK_ASPECT = parseFloat(cfg.getAttribute('data-deck-aspect') || '0.6') || 0.6;
  var STATE_POST_MS = 300;  // how often the logo position is shared while up
  var PHOTO_MS = 25000;   // how long each slideshow photo stays up
  var FADE_MS = 2000;     // crossfade length between photos
  var lastActivity = Date.now();
  var overlay = null;
  var clockTimer = null;
  var rafId = null;
  var photoTimer = null;

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function updateClock() {
    if (!overlay) return;
    var now = new Date();
    var t = overlay.querySelector('.ss-time');
    var d = overlay.querySelector('.ss-date');
    if (t) t.textContent = pad(now.getHours()) + ':' + pad(now.getMinutes());
    if (d) d.textContent = now.toLocaleDateString(undefined, {
      weekday: 'long', month: 'long', day: 'numeric',
    });
  }

  // Post the shared saver state (the logo mark's box, panel-normalized) so
  // the Stream Deck can render its slice of the canvas. A reply carrying
  // dismiss=true means a deck key press ended the saver: hide it here too.
  function postSaverState(body) {
    fetch('ui/screensaver/state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive: true,
      cache: 'no-store',
    }).then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.dismiss && overlay) hide();
      })
      .catch(function () { });
  }

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

  // Off-screen band for the deck's side of the canvas, in layout px. For
  // above/below the deck's width spans the panel width, so the band is that
  // width times the deck grid's height/width ratio; left/right is the same
  // idea against the panel height. 0 when no deck is in the canvas.
  function deckBandPx(w, h) {
    if (DECK_LAYOUT === 'above' || DECK_LAYOUT === 'below') return w * DECK_ASPECT;
    if (DECK_LAYOUT === 'left' || DECK_LAYOUT === 'right') return h / DECK_ASPECT;
    return 0;
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
  //
  // With a Stream Deck in the canvas (DECK_LAYOUT not 'off'), the wall on the
  // deck's side moves out by a band sized from the deck's key grid, so the
  // logo glides off the panel, across the deck, and back.
  function startBounce(block, mark) {
    var x = null, y = null, dx = 0, dy = 0, last = null;
    var lastPost = 0;
    bounceActive = true;  // this loop drives the timer pills too
    function step(ts) {
      if (!overlay) return;
      var vp = layoutSize();
      var w = vp.w, h = vp.h;
      var bw = block.offsetWidth, bh = block.offsetHeight;
      var bandPx = deckBandPx(w, h);
      var vw = w + ((DECK_LAYOUT === 'left' || DECK_LAYOUT === 'right') ? bandPx : 0);
      var vh = h + ((DECK_LAYOUT === 'above' || DECK_LAYOUT === 'below') ? bandPx : 0);
      var maxX = Math.max(0, vw - bw);
      var maxY = Math.max(0, vh - bh);
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
      // Virtual coords put the deck band past the panel edge; for a deck
      // above or to the left, on-screen coordinates shift back so the panel
      // still occupies its own 0..w / 0..h.
      var sx = x - (DECK_LAYOUT === 'left' ? bandPx : 0);
      var sy = y - (DECK_LAYOUT === 'above' ? bandPx : 0);
      block.style.transform = 'translate(' + sx + 'px,' + sy + 'px)';
      // The timer pills ride this same frame step (one rAF loop total). They
      // carom off the logo block, approximated as a circle in on-screen
      // coordinates; the logo itself never changes course.
      stepTimerBodies(ts, w, h, {
        cx: sx + bw / 2, cy: sy + bh / 2, r: (bw + bh) / 4,
      });
      if (DECK_LAYOUT !== 'off' && bandPx > 0 && ts - lastPost >= STATE_POST_MS) {
        lastPost = ts;
        // Share just the raccoon mark's box (not the clock under it), in
        // panel-normalized units: the panel is 0..1 on each axis and the
        // deck band extends past that range on its side. Finished timer
        // pills ride along in the same units (FoodAssistant-07ee): only the
        // done ones, so an expired timer grabs attention on the deck too
        // while the payload stays a handful of numbers.
        postSaverState({
          active: true,
          x: (sx + (mark ? mark.offsetLeft : 0)) / w,
          y: (sy + (mark ? mark.offsetTop : 0)) / h,
          w: (mark ? mark.offsetWidth : bw) / w,
          h: (mark ? mark.offsetHeight : bh) / h,
          band: (DECK_LAYOUT === 'above' || DECK_LAYOUT === 'below')
            ? bandPx / h : bandPx / w,
          layout: DECK_LAYOUT,
          pills: donePillBoxes(w, h),
        });
      }
      rafId = requestAnimationFrame(step);
    }
    rafId = requestAnimationFrame(step);
  }

  // -- floating kitchen timers (FoodAssistant-8c6m) -------------------------

  var TIMER_POLL_MS = 3000;   // registry poll cadence while the saver is up
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
      'display:flex;align-items:center;gap:1.4vmin;padding:1.1vmin 2.4vmin;' +
      'border-radius:999px;background:rgba(24,26,32,0.88);' +
      'border:1px solid rgba(255,255,255,0.18);color:#e8eaed;' +
      'white-space:nowrap;';
    var iconChar = timerFoodIcon(t.label);
    var icon = document.createElement('span');
    icon.className = 'ss-timer-icon';
    icon.style.cssText = 'font-size:4.2vmin;line-height:1;';
    icon.textContent = iconChar;
    var col = document.createElement('div');
    col.style.cssText = 'text-align:left;';
    var lab = document.createElement('div');
    lab.className = 'ss-timer-label';
    lab.style.cssText = 'font-size:2.1vmin;opacity:0.75;max-width:34vmin;' +
      'overflow:hidden;text-overflow:ellipsis;';
    lab.textContent = t.label || 'Timer';
    var time = document.createElement('div');
    time.className = 'ss-timer-time';
    time.style.cssText = 'font-size:3.4vmin;font-weight:600;line-height:1.15;' +
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
      // past the edge: reflect exactly at the wall, like the logo does. A
      // DONE pill's walls extend into the deck band the same way the logo's
      // do (bounce mode only, the mode that drives the shared canvas), so a
      // finished timer physically drifts across the seam onto the deck keys;
      // running pills keep panel-only walls (FoodAssistant-07ee).
      var band = (b.done && bounceActive) ? deckBandPx(w, h) : 0;
      var minX = DECK_LAYOUT === 'left' ? -band : 0;
      var minY = DECK_LAYOUT === 'above' ? -band : 0;
      var maxX = Math.max(minX, w - b.w + (DECK_LAYOUT === 'right' ? band : 0));
      var maxY = Math.max(minY, h - b.h + (DECK_LAYOUT === 'below' ? band : 0));
      if (b.x <= minX) { b.x = minX; b.vx = Math.abs(b.vx); }
      else if (b.x >= maxX) { b.x = maxX; b.vx = -Math.abs(b.vx); }
      if (b.y <= minY) { b.y = minY; b.vy = Math.abs(b.vy); }
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

  // The done pills' boxes for the shared state post, panel-normalized: the
  // panel is 0..1 on each axis and a pill inside the deck band sits past that
  // range, exactly like the mark's box. Only FINISHED pills are shared (the
  // point is attention for an expired timer), capped so the 300ms post stays
  // a handful of numbers.
  var PILL_SHARE_CAP = 4;
  function donePillBoxes(w, h) {
    var out = [];
    for (var i = 0; i < timerBodies.length && out.length < PILL_SHARE_CAP; i++) {
      var b = timerBodies[i];
      if (!b.done) continue;
      out.push({ id: b.id, x: b.x / w, y: b.y / h, w: b.w / w, h: b.h / h,
                 done: true, icon: b.icon });
    }
    return out;
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
    startBounce(block, mark);
  }

  // Photo slideshow. Each image is cover-fit and drifts with a slow Ken Burns
  // pan/zoom (a long CSS transform transition, so the compositor does the
  // work); the next image crossfades in on its own layer. Order is shuffled
  // per saver run, reshuffled when the deck runs out. EXIF rotation is the
  // browser's job (image-orientation: from-image). The corner clock moves to
  // a different corner with every photo as the burn-in guard.
  function startPhotosMode(names) {
    if (!overlay) return;
    var order = names.slice();
    for (var i = order.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = order[i]; order[i] = order[j]; order[j] = t;
    }
    var idx = 0;
    var current = null;
    var corners = ['right:3vmin;bottom:3vmin;', 'left:3vmin;bottom:3vmin;',
                   'left:3vmin;top:3vmin;', 'right:3vmin;top:3vmin;'];
    var cornerIdx = 0;

    var clock = document.createElement('div');
    clock.className = 'ss-time';
    clock.style.cssText =
      'position:absolute;' + corners[0] + 'z-index:3;font-size:3.5vmin;' +
      'font-weight:600;color:#e8eaed;text-shadow:0 0 1.2vmin rgba(0,0,0,0.9);';
    overlay.appendChild(clock);
    updateClock();
    startTimerLoop();  // photos have no rAF of their own; the pills need one

    function kenBurns(img) {
      // Random start/end offsets small enough that the 1.12x scale always
      // keeps the frame covered; linear so the drift never visibly stops.
      function off() { return ((Math.random() * 6) - 3).toFixed(2) + '%'; }
      img.style.transform = 'scale(1.12) translate(' + off() + ',' + off() + ')';
      img.getBoundingClientRect(); // commit the start frame
      img.style.transition = 'opacity ' + FADE_MS + 'ms ease, transform ' +
        (PHOTO_MS + FADE_MS * 2) + 'ms linear';
      img.style.transform = 'scale(1.12) translate(' + off() + ',' + off() + ')';
    }

    function advance() {
      if (!overlay) return;
      var name = order[idx];
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
        clock.style.cssText =
          'position:absolute;' + corners[cornerIdx] + 'z-index:3;font-size:3.5vmin;' +
          'font-weight:600;color:#e8eaed;text-shadow:0 0 1.2vmin rgba(0,0,0,0.9);';
        if (old) {
          old.style.opacity = '0';
          setTimeout(function () {
            if (old.parentNode) old.parentNode.removeChild(old);
          }, FADE_MS + 200);
        }
        photoTimer = setTimeout(advance, PHOTO_MS);
      };
      img.onerror = function () {
        // A vanished file (drive pulled mid-show) just skips ahead.
        if (img.parentNode) img.parentNode.removeChild(img);
        photoTimer = setTimeout(advance, 1000);
      };
      img.src = 'ui/screensaver/photo?name=' + encodeURIComponent(name);
      overlay.insertBefore(img, clock);
    }
    advance();
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
      'position:fixed;inset:0;z-index:2147483000;background:#000;' +
      'opacity:0;transition:opacity 1.2s ease;cursor:none;overflow:hidden;';
    document.body.appendChild(overlay);
    // Fade in on the next frame so the transition runs.
    requestAnimationFrame(function () {
      if (overlay) overlay.style.opacity = '1';
    });
    clockTimer = setInterval(updateClock, 5000);
    // Active timers ride the saver as bouncing pills: poll the shared
    // registry only while the saver is showing, starting right away so the
    // pills appear with the fade-in.
    pollTimers();
    timerPollId = setInterval(pollTimers, TIMER_POLL_MS);
    if (MODE === 'photos') {
      // The list is fetched fresh at every saver start so plugging in or
      // pulling the drive takes effect on the next idle, no restart needed.
      fetch('ui/screensaver/photos', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!overlay) return; // dismissed while fetching
          var names = data && data.photos;
          if (Array.isArray(names) && names.length) startPhotosMode(names);
          else startBounceMode();
        })
        .catch(function () { if (overlay) startBounceMode(); });
    } else {
      startBounceMode();
    }
  }

  function hide() {
    if (!overlay) return;
    var el = overlay;
    overlay = null;
    // Tell the deck the saver ended so it returns to its keys promptly.
    if (DECK_LAYOUT !== 'off') postSaverState({ active: false });
    clearInterval(clockTimer);
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    if (photoTimer) clearTimeout(photoTimer);
    photoTimer = null;
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

  // Settings page Test button (FoodAssistant-fiwc): force the saver on now,
  // optionally previewing a speed/style straight from the form so choices can
  // be checked before (or after) saving. The override sticks for this page
  // load only; a reload re-reads the saved settings.
  window.__screensaverTest = function (opts) {
    opts = opts || {};
    if (opts.speed && SPEEDS[opts.speed]) SPEED = SPEEDS[opts.speed];
    // Exact pixels-per-second override, used by the automated wall probe so a
    // test run can cross the screen in a couple of seconds.
    if (opts.speedPx > 0) SPEED = opts.speedPx;
    if (opts.mode) MODE = opts.mode === 'photos' ? 'photos' : 'bounce';
    motionGraceUntil = Date.now() + 1500;
    show();
  };
})();
