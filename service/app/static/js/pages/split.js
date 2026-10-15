// Split-screen kiosk wrapper (templates/split.html).

(function () {
  var primary = document.getElementById('pane-primary');
  var secondary = document.getElementById('pane-secondary');

  // Activity relay: input inside an iframe never reaches the wrapper, so
  // without this the wrapper's screensaver would count someone actively
  // using a pane as idle. On every pane load (a fresh document each
  // navigation) listeners go onto the pane's window and forward activity
  // as a synthetic mousemove on the wrapper, which screensaver.js already
  // treats as activity. Throttled; same-origin, so the try never fires.
  var EVENTS = ['pointerdown', 'touchstart', 'mousedown', 'keydown',
                'mousemove', 'wheel'];
  var lastRelay = 0;
  function relay() {
    var now = Date.now();
    if (now - lastRelay < 1000) return;
    lastRelay = now;
    try { window.dispatchEvent(new Event('mousemove')); } catch (e) { }
  }
  function wireActivity(frame) {
    if (!frame) return;
    frame.addEventListener('load', function () {
      try {
        var win = frame.contentWindow;
        for (var i = 0; i < EVENTS.length; i++) {
          win.addEventListener(EVENTS[i], relay,
                               { capture: true, passive: true });
        }
      } catch (e) { /* surprise cross-origin page: skip the relay */ }
    });
  }
  wireActivity(primary);
  wireActivity(secondary);

  // Locked pane (FoodAssistant-dqpq): the first pane's page is remembered
  // from its first load, and any navigation that carries the pane to a
  // different page snaps it straight back, so a stray tap can never lose
  // the always-on panel. location.replace keeps the pane's history flat.
  if (primary && primary.getAttribute('data-locked') === '1') {
    var homeHref = null;
    var homePath = null;
    primary.addEventListener('load', function () {
      try {
        var loc = primary.contentWindow.location;
        if (homeHref === null) {
          homeHref = loc.href;
          homePath = loc.pathname;
          return;
        }
        if (loc.pathname !== homePath) loc.replace(homeHref);
      } catch (e) { /* cross-origin: leave it; the pane stays usable */ }
    });
  }
})();
