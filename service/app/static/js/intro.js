// Kiosk intro animation (FoodAssistant-a8xy).
//
// On a kiosk, the first page load after a boot opens on a short branded
// intro: a near-black screen, the raccoon mark fading and scaling in with a
// gentle settle, holding a beat, then dissolving into the app. Roughly two
// seconds end to end; any touch or key press skips it instantly, and the
// skipping tap is swallowed (same trick as screensaver.js) so it never
// presses whatever sits underneath.
//
// Once per boot: sessionStorage survives page navigations but not a kiosk
// restart, so the flag set here means moving around the app never replays
// the intro, while a reboot shows it again. Non-kiosk browsers never see it.
//
// The templates run a tiny synchronous guard before this script loads: when
// the intro is going to play, it blacks out the page pre-render (an
// html.intro-pending class plus a #intro-blackout style) so the app never
// flashes for a frame before the overlay appears. This script owns removing
// that blackout: immediately when the intro is skipped or not due, or as
// soon as the overlay covers the page when it plays.
//
// Animations are transform/opacity only (compositor-friendly on a Pi), and
// prefers-reduced-motion drops the scale so the logo only fades.
(function () {
  var shouldRun = false;
  try {
    shouldRun = localStorage.getItem('kioskMode') === 'true' &&
                !sessionStorage.getItem('kioskIntroShown');
  } catch (e) { /* no storage / private mode: never intro */ }

  function clearBlackout() {
    document.documentElement.classList.remove('intro-pending');
    var s = document.getElementById('intro-blackout');
    if (s && s.parentNode) s.parentNode.removeChild(s);
  }

  if (!shouldRun) {
    // The guard and this script agree on the condition, so normally there is
    // no blackout to clear here; this covers storage changing between the
    // two (for example another tab finishing the intro first).
    clearBlackout();
    return;
  }
  try { sessionStorage.setItem('kioskIntroShown', '1'); } catch (e) {}

  var reduced = false;
  try {
    reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) {}

  // Timeline in ms: the mark comes in with a slight overshoot and settles,
  // holds a beat, then the whole overlay dissolves into the app. Total 2.2s.
  var MARK_IN = 900;
  var HOLD = 700;
  var OUT = 600;

  var overlay = null;
  var outTimer = null;
  var endTimer = null;
  var suppressUntil = 0;
  var SWALLOW = ['pointerdown', 'pointerup', 'touchstart', 'touchend',
                 'mousedown', 'mouseup', 'click', 'keydown'];

  function removeOverlay() {
    if (!overlay) return;
    var el = overlay;
    overlay = null;
    if (outTimer) clearTimeout(outTimer);
    if (endTimer) clearTimeout(endTimer);
    if (el.parentNode) el.parentNode.removeChild(el);
    var style = document.getElementById('kiosk-intro-style');
    if (style && style.parentNode) style.parentNode.removeChild(style);
  }

  function dissolve() {
    if (!overlay) return;
    overlay.style.transition = 'opacity ' + OUT + 'ms ease';
    overlay.style.opacity = '0';
    endTimer = setTimeout(removeOverlay, OUT + 100);
  }

  // A skipping tap or key press drops the overlay instantly and keeps
  // swallowing the rest of its gesture (the pointerup/touchend/click that
  // follow) so it never presses what is underneath.
  function onInput(ev) {
    if (overlay) {
      suppressUntil = Date.now() + 700;
      if (ev.cancelable) ev.preventDefault();
      ev.stopPropagation();
      removeOverlay();
      return;
    }
    if (Date.now() < suppressUntil) {
      if (ev.cancelable) ev.preventDefault();
      ev.stopPropagation();
    }
  }
  for (var i = 0; i < SWALLOW.length; i++) {
    // Capture phase so the skip is seen (and swallowed) before the page's
    // own handlers. passive:false lets preventDefault work on touch.
    window.addEventListener(SWALLOW[i], onInput, { capture: true, passive: false });
  }

  function start() {
    var style = document.createElement('style');
    style.id = 'kiosk-intro-style';
    style.textContent =
      // Gentle settle: a small overshoot on the way in, nothing bouncy.
      '@keyframes kiosk-intro-mark {' +
      '  0% { opacity: 0; transform: scale(0.82); }' +
      '  70% { opacity: 1; transform: scale(1.04); }' +
      '  100% { opacity: 1; transform: scale(1); }' +
      '}' +
      '@keyframes kiosk-intro-mark-fade {' +
      '  0% { opacity: 0; }' +
      '  100% { opacity: 1; }' +
      '}';
    document.head.appendChild(style);

    overlay = document.createElement('div');
    overlay.id = 'kiosk-intro';
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:2147483100;background:#050505;' +
      'display:flex;align-items:center;justify-content:center;' +
      'opacity:1;cursor:none;overflow:hidden;';
    var mark = document.createElement('img');
    mark.src = 'static/icons/logo-mark.png';
    mark.alt = '';
    mark.style.cssText =
      'width:34vmin;height:34vmin;will-change:transform,opacity;' +
      'animation:' + (reduced ? 'kiosk-intro-mark-fade' : 'kiosk-intro-mark') +
      ' ' + MARK_IN + 'ms ease-out both;';
    overlay.appendChild(mark);
    document.body.appendChild(overlay);
    // The overlay now covers the page, so the pre-render blackout can go.
    clearBlackout();
    outTimer = setTimeout(dissolve, MARK_IN + HOLD);
  }

  // This script loads from <head>, so the body may not exist yet; the guard's
  // blackout keeps the screen dark until the overlay is attached.
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
