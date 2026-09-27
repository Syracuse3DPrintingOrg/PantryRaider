/*
 * Auto-return to the home page after inactivity on a kiosk (FoodAssistant-6e5m).
 *
 * A walk-up kiosk left on a sub-page (someone checked a recipe or a forecast,
 * then walked away) should drift back to the home screen so the next person
 * starts fresh. After the configured idle seconds with no touch, this navigates
 * to whatever leads the nav (the same home the brand logo links to).
 *
 * Guards, so the screen never jumps away when it should not:
 *   - kiosk mode only (no-op in an ordinary browser),
 *   - only when the feature is enabled and a positive timeout is set,
 *   - never on an EXEMPT page (Cook / On the Line, Weather, Cameras, Timers by
 *     default): you do not want the view yanked away mid-cook or mid-forecast,
 *   - never when already on the home page (it would pointlessly reload),
 *   - the idle clock resets on the same real-touch events the screensaver uses,
 *     and pauses while the tab is hidden (e.g. the screensaver is up).
 */
(function () {
  var cfg = document.getElementById('kiosk-home-config');
  if (!cfg) return;
  if (cfg.getAttribute('data-enabled') !== '1') return;

  // Kiosk panels only: an ordinary browser visiting the app must never be
  // navigated on its own. Mirrors the gate the other kiosk scripts use.
  try {
    if (window.localStorage.getItem('kioskMode') !== 'true') return;
  } catch (e) {
    return;
  }

  var seconds = parseInt(cfg.getAttribute('data-seconds'), 10);
  if (!isFinite(seconds) || seconds <= 0) return;

  var active = (cfg.getAttribute('data-active') || '').trim();
  var home = (cfg.getAttribute('data-home') || 'ui/').trim();
  var exempt = (cfg.getAttribute('data-exempt') || '')
    .split(',').map(function (s) { return s.trim(); })
    .filter(function (s) { return s.length > 0; });

  // Current page is exempt: leave it be.
  if (active && exempt.indexOf(active) !== -1) return;

  // Already home: nothing to return to. Compare the path we are on against the
  // home target (both root-relative, and the kiosk runs on loopback with no
  // ingress prefix, so a suffix match is enough and tolerant of a trailing /).
  var here = location.pathname.replace(/\/+$/, '');
  var target = home.replace(/^\/+/, '').replace(/\/+$/, '');
  if (target && (here === '/' + target || here.slice(-(target.length + 1)) === '/' + target)) return;

  var last = Date.now();
  function reset() { last = Date.now(); }

  // Same real-interaction events kiosk-idle.js counts (deliberately no
  // mousemove: the hidden compositor cursor drifts and would never let the
  // screen settle). Passive so scrolling/taps stay smooth.
  var events = ['pointerdown', 'touchstart', 'keydown', 'wheel', 'click'];
  for (var i = 0; i < events.length; i++) {
    window.addEventListener(events[i], reset, { passive: true });
  }

  setInterval(function () {
    // Do not count idle time while the tab is hidden (the screensaver layer or a
    // blanked panel); resume the clock when it comes back.
    if (document.hidden) { reset(); return; }
    if (Date.now() - last >= seconds * 1000) {
      window.location.href = home;
    }
  }, 1000);
})();
