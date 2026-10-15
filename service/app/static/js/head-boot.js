// Head-of-page setup for every base.html page, loaded plain (not deferred)
// so it runs before first paint.

// Register the PWA service worker for an offline app shell and installability
// (FoodAssistant-fd3z). Guarded two ways: the browser must support workers,
// and the page must be a secure context (https) or localhost. Over plain-http
// on the LAN a worker cannot register, so this is a deliberate no-op there and
// the app behaves exactly as it does today. Registered from the origin root
// with scope "/" so it can control every page. Under Home Assistant Ingress the
// origin root belongs to Home Assistant, so the page passes data-sw="off" and
// no worker registers (FoodAssistant-iaa0s).
(function () {
  var me = document.currentScript;
  if (me && me.getAttribute('data-sw') === 'off') return;
  if ('serviceWorker' in navigator &&
      (window.isSecureContext ||
       location.hostname === 'localhost' ||
       location.hostname === '127.0.0.1')) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }
})();

// Kiosk mode: load touch-target CSS before first paint to avoid reflow flash.
// The stylesheet address (with its cache-busting version) comes from the
// script tag's data-kiosk-css.
(function () {
  var me = document.currentScript;
  var href = me && me.getAttribute('data-kiosk-css');
  try {
    if (href && localStorage.getItem('kioskMode') === 'true') {
      var link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      document.head.appendChild(link);
    }
  } catch (e) { /* no storage: not a kiosk */ }
})();
