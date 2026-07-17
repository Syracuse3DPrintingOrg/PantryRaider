// Kiosk presence indicator (FoodAssistant-99vy).
//
// An on-glass readout for the mmWave presence sensor (LD2410C on GPIO17): a
// small ghosted person icon that lights up, with a gentle pulse, while the
// sensor reads presence. Its whole point is verifying a sensor install by
// standing at the device and watching the icon follow you, no SSH needed, so
// it doubles as an always-available "the sensor sees me" cue.
//
// Gates, in order:
//   - base.html only loads this script when the Settings toggle is on
//     (Display & Sleep, next to Wake on presence);
//   - kiosk mode only, via the same localStorage latch every kiosk script
//     checks (see kiosk-display.js, which sets it from the ?kiosk=1 boot URL);
//   - the indicator mounts only once the host bridge reports the sensor has
//     actually fired (presence_ever_high, or a live presence_detected), so a
//     kiosk with no sensor wired shows nothing at all, and the icon appears
//     the first time a real sensor sees someone (readability alone is true on
//     every Pi and is not enough, FoodAssistant-77ao).
//
// The poll uses the RELATIVE setup/kiosk/activity URL like the other kiosk
// pollers (kiosk-idle.js), so an ingress path prefix survives. The device's
// own kiosk display reaches /setup through the loopback trust; a REMOTE
// kiosk-latched browser holding only a viewer session gets 403 from the
// /setup admin gate, the poll fails, and the indicator simply stays hidden
// there. That is acceptable: this is a device-local hardware readout, and no
// auth bypass is worth adding for it.
(function () {
  if (localStorage.getItem('kioskMode') !== 'true') return;

  var POLL_MS = 2000;
  var mounted = false;
  var el = null;
  var styled = false;

  function ensureStyle() {
    if (styled) return;
    styled = true;
    var style = document.createElement('style');
    style.textContent =
      // Corner choice: the HA event toasts own the top-right and timer chips
      // sit in the bottom corner, so top-left, just below the fixed top bar,
      // is the spot none of the overlay siblings claim, on the portrait
      // 480x800 Bandit panel and the 1024x600 landscape panels alike. One
      // sibling CAN land here: a LEFT-docked floating nav is a full-height
      // opaque bar at a higher z-index, so the icon also offsets by the
      // --float-nav-left width that floating-nav.js publishes exactly for
      // fixed widgets like this one (0px when the nav is elsewhere). The
      // other offsets add the kiosk safe-area insets published by
      // kiosk-display.js (falling back to the browser's own safe-area
      // env()), so the icon stays on the visible glass on a panel whose
      // viewport draws wider than it shows.
      '#pr-presence-indicator{' +
        'position:fixed;' +
        'top:calc(var(--kiosk-inset-top, env(safe-area-inset-top, 0px)) + 64px);' +
        'left:calc(var(--kiosk-inset-left, env(safe-area-inset-left, 0px)) + ' +
          'var(--float-nav-left, 0px) + 10px);' +
        // Above page content, below the navbar/menus (1030+) and every
        // overlay sibling, so the screensaver and pop-ups cover it cleanly.
        'z-index:1025;' +
        'width:24px;height:24px;color:#9aa4af;opacity:.22;' +
        // Never eat a touch: the indicator is purely visual and must not
        // steal a tap from whatever sits under it.
        'pointer-events:none;' +
        'transition:opacity .5s ease;}' +
      '#pr-presence-indicator svg{display:block;width:100%;height:100%;}' +
      '#pr-presence-indicator.pr-presence-on{' +
        'opacity:.95;color:#F2006E;' +
        'animation:pr-presence-pulse 1.6s ease-in-out infinite;}' +
      '@keyframes pr-presence-pulse{' +
        '0%,100%{transform:scale(1);}50%{transform:scale(1.2);}}' +
      // prefers-reduced-motion: drop the pulse; the ghosted-to-lit opacity
      // and color change alone carry the signal.
      '@media (prefers-reduced-motion: reduce){' +
        '#pr-presence-indicator.pr-presence-on{animation:none;}}';
    document.head.appendChild(style);
  }

  function mount() {
    if (mounted) return;
    ensureStyle();
    el = document.createElement('div');
    el.id = 'pr-presence-indicator';
    el.setAttribute('aria-hidden', 'true');
    // A simple person outline drawn in currentColor: ghosted grey when idle,
    // brand pink when the sensor reads presence.
    el.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
      ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<circle cx="12" cy="7" r="3.4"/>' +
      '<path d="M5.5 21c.6-4.2 3.2-6.5 6.5-6.5s5.9 2.3 6.5 6.5"/>' +
      '</svg>';
    (document.body || document.documentElement).appendChild(el);
    mounted = true;
  }

  function unmount() {
    if (!mounted) return;
    if (el && el.parentNode) el.parentNode.removeChild(el);
    el = null;
    mounted = false;
  }

  function apply(data) {
    // The mount gate is "a sensor has actually fired", not merely
    // "the GPIO is readable" (FoodAssistant-77ao). presence_readable is true
    // on EVERY Pi the moment the line is initialized, whether or not an
    // LD2410C is wired (GPIO17 is pulled down and simply reads low with no
    // sensor), so gating on it alone put a ghost icon on every sensor-less
    // kiosk and, since a readable answer resets the backoff, polled forever.
    // presence_ever_high latches true only when the line has actually gone
    // high, so it is false on a bare pull-down pin and true once a real sensor
    // sees someone: the icon appears the first time the sensor proves itself,
    // then follows presence. Still hidden off a Pi, on an older bridge without
    // the field, on an error reply, or when Wake on presence is "off" (the
    // bridge's presence loop idles there and the reading freezes; a stale
    // value shown as live is worse than none).
    var sensorSeen = !!data
        && (data.presence_ever_high === true || data.presence_detected === true);
    if (!data || data.ok !== true || data.presence_readable !== true
        || data.wake_on_presence === 'off' || !sensorSeen) {
      unmount();
      return false;
    }
    mount();
    if (data.presence_detected) {
      el.classList.add('pr-presence-on');
    } else {
      el.classList.remove('pr-presence-on');
    }
    return true;
  }

  // Poll idiom shared with ha-events.js: chained setTimeout (never
  // setInterval), so a slow proxy answer can never stack concurrent
  // requests; nothing is fetched while the tab is hidden; and consecutive
  // failures (a 403 on a remote viewer session, a bridge that is down) back
  // the cadence off to BACKOFF_MAX_MS instead of hammering forever. Any
  // successful, mountable answer snaps the cadence back to POLL_MS.
  var BACKOFF_MAX_MS = 30000;
  var delay = POLL_MS;

  function schedule() {
    setTimeout(poll, delay);
  }

  function poll() {
    if (document.hidden) {
      schedule();
      return;
    }
    fetch('setup/kiosk/activity', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        delay = apply(data) ? POLL_MS
                            : Math.min(delay * 2, BACKOFF_MAX_MS);
      })
      // A failing poll (network hiccup, an auth bounce mid-session) unmounts
      // rather than freezing a stale reading on screen, and backs off too.
      .catch(function () {
        unmount();
        delay = Math.min(delay * 2, BACKOFF_MAX_MS);
      })
      .finally(schedule);
  }

  // When the tab becomes visible again, snap the cadence back so the poll
  // chain (which keeps ticking while hidden, skipping the fetches) returns
  // to the fast rhythm within one backed-off interval at most.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { delay = POLL_MS; }
  });

  poll();
})();
