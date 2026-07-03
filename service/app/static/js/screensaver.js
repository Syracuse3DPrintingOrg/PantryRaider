// Kiosk screensaver (FoodAssistant-y65x, photos FoodAssistant-5w4m).
//
// Runs only in kiosk mode. After the configured idle minutes the page fades to
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
(function () {
  var kiosk = false;
  try {
    kiosk = localStorage.getItem('kioskMode') === 'true';
  } catch (e) { /* no storage / private mode: idle activation never runs */ }

  var cfg = document.getElementById('screensaver-config');
  if (!cfg) return;
  var minutes = parseInt(cfg.getAttribute('data-minutes') || '0', 10);

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

  // Old-school DVD bounce: the block travels in a dead-straight line at
  // constant speed until it HITS an edge, then reflects (angle in = angle
  // out) and carries on; nothing else ever changes its course. Frame-time
  // based so the speed is identical on a slow Pi and a fast desktop, and the
  // block size is measured each frame so a viewport resize or font load just
  // tightens the walls without a jump. Transform keeps motion compositor-side.
  //
  // Every measurement here lives in LAYOUT pixels, the space the translate()
  // coordinates render in: the visual viewport divided by the kiosk interface
  // scale's zoom for the walls, and offsetWidth/Height for the block. Mixing
  // window.innerWidth (visual pixels) with translate coordinates (layout
  // pixels) broke the walls whenever a zoom applied, stopping the bounce
  // short of (or past) the right and bottom edges (FoodAssistant-vf4f).
  //
  // With a Stream Deck in the canvas (DECK_LAYOUT not 'off'), the wall on the
  // deck's side moves out by a band sized from the deck's key grid, so the
  // logo glides off the panel, across the deck, and back.
  function startBounce(block, mark) {
    var x = null, y = null, dx = 0, dy = 0, last = null;
    var lastPost = 0;
    function step(ts) {
      if (!overlay) return;
      // Effective zoom from the kiosk interface scale (kiosk-display.js sets
      // html.style.zoom); 1 everywhere else. Read each frame so a live scale
      // change just tightens the walls without a jump.
      var z = 1;
      try {
        z = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
      } catch (e) { /* keep 1 */ }
      var w = window.innerWidth / z, h = window.innerHeight / z;
      var bw = block.offsetWidth, bh = block.offsetHeight;
      // Off-screen band for the deck's side of the canvas, in layout px. For
      // above/below the deck's width spans the panel width, so the band is
      // that width times the deck grid's height/width ratio; left/right is
      // the same idea against the panel height.
      var bandPx = 0;
      if (DECK_LAYOUT === 'above' || DECK_LAYOUT === 'below') {
        bandPx = w * DECK_ASPECT;
      } else if (DECK_LAYOUT === 'left' || DECK_LAYOUT === 'right') {
        bandPx = h / DECK_ASPECT;
      }
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
      if (DECK_LAYOUT !== 'off' && bandPx > 0 && ts - lastPost >= STATE_POST_MS) {
        lastPost = ts;
        // Share just the raccoon mark's box (not the clock under it), in
        // panel-normalized units: the panel is 0..1 on each axis and the
        // deck band extends past that range on its side.
        postSaverState({
          active: true,
          x: (sx + (mark ? mark.offsetLeft : 0)) / w,
          y: (sy + (mark ? mark.offsetTop : 0)) / h,
          w: (mark ? mark.offsetWidth : bw) / w,
          h: (mark ? mark.offsetHeight : bh) / h,
          band: (DECK_LAYOUT === 'above' || DECK_LAYOUT === 'below')
            ? bandPx / h : bandPx / w,
          layout: DECK_LAYOUT,
        });
      }
      rafId = requestAnimationFrame(step);
    }
    rafId = requestAnimationFrame(step);
  }

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
    style.textContent = 'body.ss-active, body.ss-active * { cursor: none !important; }';
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

  // Idle activation only runs on a kiosk with a timeout configured; the test
  // hook below works everywhere the script loads.
  if (kiosk && IDLE_MS > 0) {
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
