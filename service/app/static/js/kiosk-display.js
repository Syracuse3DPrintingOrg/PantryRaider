/*
 * Kiosk display settings (scale + orientation).
 *
 * These apply ONLY to a hardware screen running in kiosk mode (the Pi's
 * attached HDMI panel), never to a regular desktop browser. A browser becomes
 * a kiosk by either:
 *   - loading the UI with ?kiosk=1 (how the appliance kiosk service launches), or
 *   - toggling kiosk mode from the nav bar.
 * The latched flag lives in localStorage, so it is per device.
 *
 * Scale is a document zoom (it reflows the layout so text wraps to the panel).
 * Orientation rotates the whole page for a physically turned screen; 90/270
 * swap width and height. The screen is non-touch, so pointer mapping after a
 * rotation is not a concern.
 */
(function () {
  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get('kiosk') === '1') {
      localStorage.setItem('kioskMode', 'true');
    }
  } catch (e) { /* private mode / no storage: fall through */ }

  if (localStorage.getItem('kioskMode') !== 'true') return;

  var html = document.documentElement;

  // Display scale and rotation are settings for the appliance's OWN attached
  // panel, not for whoever opens the UI. On a Pi, only the local display
  // (reached over loopback, since the kiosk service loads http://localhost/...)
  // applies them; a remote browser served by the same Pi must never inherit the
  // panel's scale or rotation, even if it carries a stale kiosk flag
  // (FoodAssistant-anou). Off a Pi there is no attached panel, so a kiosk-mode
  // browser keeps applying its own scale as before.
  var host = location.hostname;
  var isLoopback = host === 'localhost' || host === '127.0.0.1' || host === '::1';
  if (html.getAttribute('data-is-pi') === '1' && !isLoopback) return;

  var scale = parseFloat(html.getAttribute('data-ui-scale') || '1') || 1;
  var rot = parseInt(html.getAttribute('data-display-rotation') || '0', 10) || 0;

  if (scale && scale !== 1) html.style.zoom = scale;

  // On the Pi appliance the compositor rotates the whole output
  // (WLR_OUTPUT_TRANSFORM), so applying an in-page CSS rotation here would
  // double it. Scale (zoom) still applies; rotation is a no-op in that mode.
  if (html.getAttribute('data-rotation-mode') === 'compositor') rot = 0;

  if (!rot) return;

  function applyRotation() {
    var wrap = document.createElement('div');
    wrap.id = 'kiosk-rotate';
    while (document.body.firstChild) wrap.appendChild(document.body.firstChild);
    document.body.appendChild(wrap);
    document.body.style.margin = '0';
    document.body.style.overflow = 'hidden';

    var vw = window.innerWidth, vh = window.innerHeight;
    var s = wrap.style;
    s.position = 'absolute';
    s.top = '0';
    s.left = '0';
    s.overflow = 'auto';
    s.transformOrigin = 'top left';

    if (rot === 180) {
      s.width = vw + 'px';
      s.height = vh + 'px';
      s.transform = 'translate(' + vw + 'px,' + vh + 'px) rotate(180deg)';
    } else if (rot === 90) {
      s.width = vh + 'px';
      s.height = vw + 'px';
      s.transform = 'translate(' + vw + 'px,0) rotate(90deg)';
    } else if (rot === 270) {
      s.width = vh + 'px';
      s.height = vw + 'px';
      s.transform = 'translate(0,' + vh + 'px) rotate(270deg)';
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyRotation);
  } else {
    applyRotation();
  }
})();
