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

  // Advanced display: per-edge safe-area insets (FoodAssistant Bandit clip).
  //
  // The kiosk browser can lay out into a viewport WIDER than the panel actually
  // shows. On the Bandit's portrait DSI panel the compositor rotates an 800x480
  // output to 480x800, but Chromium reports window.innerWidth 500 on that 480px
  // screen, so the right ~20 CSS px render off the panel and clip the flush-right
  // chrome (the hamburger, the Screensaver button, a card's close X). No in-page
  // overflow guard can catch it: from the browser's side nothing overflows, the
  // lost pixels live in the compositor -> panel mapping, past everything CSS sees.
  //
  // Two inset sources are added together, per edge:
  //   - an AUTOMATIC term for exactly the case above: whatever the CSS viewport
  //     has beyond the physical screen (innerWidth - screen.width, and the same
  //     vertically). screen.width/height are the visible panel; this is 0 on a
  //     panel whose viewport already fits, so it changes nothing there.
  //   - the USER margins (data-display-margin-*), a manual nudge for a panel that
  //     also hides a rim behind its bezel, which the browser cannot detect.
  //
  // The insets are published as CSS custom properties that kiosk.css applies:
  // body padding pulls in normal-flow content, and an explicit left/right/top on
  // the FIXED navbar and floating nav pulls those in too (padding alone never
  // moves a position:fixed element, which is why earlier fixes missed the navbar).
  // The properties are consumed inside the zoomed <html>, so each value is
  // pre-divided by the scale to land the requested DEVICE pixels at any ui_scale.
  (function () {
    function marginAttr(name) {
      var v = parseInt(html.getAttribute(name) || '0', 10);
      if (!isFinite(v) || v < 0) return 0;
      return v > 200 ? 200 : v;  // mirror config.clamp_display_margin
    }
    var mT = marginAttr('data-display-margin-top');
    var mR = marginAttr('data-display-margin-right');
    var mB = marginAttr('data-display-margin-bottom');
    var mL = marginAttr('data-display-margin-left');
    var sw = (window.screen && screen.width) || 0;
    var sh = (window.screen && screen.height) || 0;
    // Cap the automatic term so a mis-reported screen size can never inset the
    // whole page away; a real panel overscan is a handful of pixels.
    var autoR = (sw > 0 && window.innerWidth > sw) ? Math.min(96, window.innerWidth - sw) : 0;
    var autoB = (sh > 0 && window.innerHeight > sh) ? Math.min(96, window.innerHeight - sh) : 0;
    var insetL = mL, insetR = mR + autoR, insetT = mT, insetB = mB + autoB;
    if (insetL || insetR || insetT || insetB) {
      function px(n) { return (n / scale).toFixed(2) + 'px'; }
      var st = html.style;
      st.setProperty('--kiosk-inset-left', px(insetL));
      st.setProperty('--kiosk-inset-right', px(insetR));
      st.setProperty('--kiosk-inset-top', px(insetT));
      st.setProperty('--kiosk-inset-bottom', px(insetB));
    }
  })();

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
