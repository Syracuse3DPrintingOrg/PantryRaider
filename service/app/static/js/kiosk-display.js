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
 * On a Pi, ?kiosk=1 belongs to the appliance's OWN display: the kiosk service
 * loads http://localhost/..., so the latch is gated on a loopback hostname,
 * the same rule kiosk-auto.js uses. Without that gate, any LAN browser that
 * ever carried a stray kiosk=1 (a copied kiosk URL, a leftover latch from an
 * earlier install at the same address) became a kiosk too, and got the
 * on-screen keyboard over every text field on the setup page. A remote
 * browser that finds itself latched without an explicit nav-toggle choice is
 * unlatched here for the same reason. Off a Pi there is no attached display,
 * so ?kiosk=1 keeps working anywhere (a wall tablet pointed at a server).
 *
 * Scale is a document zoom (it reflows the layout so text wraps to the panel).
 * Orientation rotates the whole page for a physically turned screen; 90/270
 * swap width and height. The screen is non-touch, so pointer mapping after a
 * rotation is not a concern.
 */
(function () {
  var html = document.documentElement;
  var isPi = html && html.getAttribute('data-is-pi') === '1';
  var host = location.hostname;
  var isLoopback = host === 'localhost' || host === '127.0.0.1' || host === '::1';

  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get('kiosk') === '1' && (!isPi || isLoopback)) {
      // The appliance's own kiosk service always launches with ?kiosk=1, so
      // treat that as an authoritative "be a kiosk": force kiosk mode on AND
      // clear a stale explicit opt-out. Without clearing kioskExplicit, a stray
      // tap of the nav's kiosk toggle (or a mode switch that left it off) kept
      // the panel stuck in the full desktop layout across kiosk restarts, since
      // kiosk-auto defers to the explicit choice (FoodAssistant-889h).
      // On a Pi only the local display (loopback) may latch; see the header.
      localStorage.setItem('kioskMode', 'true');
      localStorage.removeItem('kioskExplicit');
    }
    // Heal a wrongly-latched remote browser on a Pi: kiosk mode that was not
    // an explicit nav-toggle choice (kioskExplicit) can only have come from a
    // stray ?kiosk=1 or an over-eager auto-enable, neither of which belongs on
    // a phone or desktop. The appliance's own display is loopback, so it is
    // never touched by this.
    if (isPi && !isLoopback
        && localStorage.getItem('kioskMode') === 'true'
        && localStorage.getItem('kioskExplicit') !== 'true') {
      localStorage.removeItem('kioskMode');
      localStorage.removeItem('kioskAuto');
    }
  } catch (e) { /* private mode / no storage: fall through */ }

  if (localStorage.getItem('kioskMode') !== 'true') return;

  // Display scale and rotation are settings for the appliance's OWN attached
  // panel, not for whoever opens the UI. On a Pi, only the local display
  // (reached over loopback, since the kiosk service loads http://localhost/...)
  // applies them; a remote browser served by the same Pi must never inherit the
  // panel's scale or rotation, even if it carries a stale kiosk flag
  // (FoodAssistant-anou). Off a Pi there is no attached panel, so a kiosk-mode
  // browser keeps applying its own scale as before.
  if (isPi && !isLoopback) return;

  var scale = parseFloat(html.getAttribute('data-ui-scale') || '1') || 1;
  var rot = parseInt(html.getAttribute('data-display-rotation') || '0', 10) || 0;

  if (scale && scale !== 1) {
    html.style.zoom = scale;
    // Zoom scales the rendering but not the layout viewport, so without
    // compensation the page lays out at the panel's full CSS width and then
    // paints scale-times wider (or narrower) than the screen: at 1.4x on a
    // 1024px panel the right ~30% hung off-screen behind kiosk.css's
    // overflow-x clip, and that clipped-but-scrollable state also broke touch
    // panning entirely (Chromium attributes the flick to the unscrollable
    // horizontal overflow). Laying out at width/scale makes layout times zoom
    // equal the viewport exactly, above and below 1: 1024/1.4 zoomed 1.4x is
    // 1024, and 1024/0.85 zoomed 0.85x is 1024 (no dead right strip either).
    html.style.width = 'calc(100% / ' + scale + ')';
  }

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
