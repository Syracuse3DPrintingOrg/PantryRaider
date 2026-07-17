// Pantry Raider kiosk return (FoodAssistant-wn8w).
//
// The kiosk browser is chromeless: a tapped external link (an Amazon listing
// from the Shop page, a recipe source, a vendor manual) fills the display with
// a site that has no way back. This content script runs on every http(s) page
// the kiosk visits and, ONLY when the page is not Pantry Raider itself, adds
// two ways home: a small floating return button, and a swipe down from the
// top edge of the screen. Both navigate to the app URL baked in by the
// provisioner (origin.js), never anywhere a page could choose.
//
// Deliberate properties:
//   * Zero cost on the app's own pages: the script returns before touching
//     the DOM when the origin matches, so kiosk pages are byte-identical.
//   * Fails closed: an unbaked or unparseable home URL means no button and
//     no gesture, never a navigation to a junk address.
//   * Page-proof: styles are set inline with !important so site CSS cannot
//     restyle the button, and a MutationObserver re-attaches it (throttled)
//     if a page's scripts remove it. The navigation target is a constant
//     captured before any page script can interfere.
//   * Listener hygiene: all touch listeners are passive, so scrolling and
//     pinch behavior on external sites is never delayed or broken.
(() => {
  "use strict";

  var HOME = (typeof __PR_KIOSK_HOME === "string") ? __PR_KIOSK_HOME : "";
  // Unbaked placeholder (or missing origin.js entirely): do nothing at all.
  if (!HOME || HOME.indexOf("__PR_KIOSK_HOME") !== -1) return;
  var home;
  try { home = new URL(HOME); } catch (e) { return; }
  if (home.protocol !== "http:" && home.protocol !== "https:") return;
  // Never on the app's own pages, and only in the top frame (the manifest
  // already says all_frames: false; this guards a future manifest edit).
  if (location.origin === home.origin) return;
  if (window.top !== window) return;

  // Captured once: the page can neither read this (isolated world) nor
  // change where the button goes.
  var HOME_HREF = home.href;
  function goHome() { location.assign(HOME_HREF); }

  var BTN_ID = "pantry-raider-return";

  function makeButton() {
    var b = document.createElement("button");
    b.id = BTN_ID;
    b.type = "button";
    b.setAttribute("aria-label", "Back to Pantry Raider");
    // A simple house glyph, inline so the extension needs no asset fetches.
    b.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" ' +
      'width="28" height="28" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 11.5 12 4l9 7.5"/>' +
      '<path d="M5.5 10.5V20h13v-9.5"/></svg>';
    // Inline !important styles beat any site stylesheet. Sized for fingers on
    // the 480x800 portrait Bandit panel and kept off the very bottom edge so
    // it clears cookie bars and store checkout footers.
    var css = {
      "position": "fixed",
      "right": "12px",
      "bottom": "84px",
      "width": "56px",
      "height": "56px",
      "border-radius": "50%",
      "border": "none",
      "padding": "0",
      "margin": "0",
      "background": "#F2006E",
      "color": "#ffffff",
      "opacity": "0.55",
      "z-index": "2147483647",
      "cursor": "pointer",
      "display": "flex",
      "align-items": "center",
      "justify-content": "center",
      "box-shadow": "0 2px 10px rgba(0,0,0,0.45)",
      "touch-action": "manipulation",
      "-webkit-tap-highlight-color": "transparent"
    };
    for (var k in css) b.style.setProperty(k, css[k], "important");
    b.addEventListener("touchstart", function () {
      b.style.setProperty("opacity", "1", "important");
    }, { passive: true });
    b.addEventListener("click", function (ev) {
      ev.preventDefault();
      goHome();
    });
    return b;
  }

  var btn = makeButton();
  function attach() {
    (document.body || document.documentElement).appendChild(btn);
  }
  attach();

  // Re-attach if a page's own scripts prune unknown nodes. Throttled to one
  // check per second so a busy SPA never turns this into a hot loop.
  var reattachPending = false;
  new MutationObserver(function () {
    if (reattachPending || btn.isConnected) return;
    reattachPending = true;
    setTimeout(function () {
      reattachPending = false;
      if (!btn.isConnected) attach();
    }, 1000);
  }).observe(document.documentElement, { childList: true, subtree: true });

  // Swipe down from the top edge of the screen. The touch must START within
  // the top edge band, travel a real distance quickly, and stay mostly
  // vertical, so scrolling a page that happens to sit at the top can never
  // trigger it by accident.
  var EDGE_PX = 24;        // the touch must begin this close to the top
  var TRAVEL_PX = 80;      // and move at least this far down
  var WINDOW_MS = 600;     // within this time
  var startX = null, startY = null, startedAt = 0;

  window.addEventListener("touchstart", function (ev) {
    var t = ev.touches && ev.touches[0];
    if (t && t.clientY <= EDGE_PX) {
      startX = t.clientX;
      startY = t.clientY;
      startedAt = Date.now();
    } else {
      startY = null;
    }
  }, { passive: true, capture: true });

  window.addEventListener("touchmove", function (ev) {
    if (startY === null) return;
    var t = ev.touches && ev.touches[0];
    if (!t) return;
    var dy = t.clientY - startY;
    var dx = Math.abs(t.clientX - startX);
    if (Date.now() - startedAt > WINDOW_MS) { startY = null; return; }
    if (dy >= TRAVEL_PX && dy > dx * 2) {
      startY = null;
      goHome();
    }
  }, { passive: true, capture: true });

  window.addEventListener("touchend", function () { startY = null; },
    { passive: true, capture: true });
})();
