// Setup-complete kiosk hand-off (kiosk only, FoodAssistant-6v9q). When setup
// is finished from a phone or computer, the Pi's attached display is left
// sitting on this page: the wizard's QR splash, or the settings page when
// the getting-ready page handed off after setup already completed. The save
// that completes setup raises a one-shot server flag; this poll picks it up
// and drives the display to the dashboard within a few seconds. Loaded on
// both the wizard and the settings branch of the page on purpose. Before
// setup completes the poll answers as JSON (see _SETUP_BYPASS in main.py);
// after, the appliance's own display is a loopback client that auth already
// trusts. A remote browser is never kiosk-latched on a Pi (kiosk-display.js
// heals that), so this runs only on the device's own display or a screen
// whose owner turned kiosk mode on, and any auth bounce lands in the catch.

(function () {
  try {
    if (localStorage.getItem('kioskMode') !== 'true') return;
  } catch (e) { return; }
  // PR.poller: a slow answer never stacks a second request, and the request
  // is dropped when the page is left.
  var poll = PR.poller(function (signal) {
    if (document.hidden) return;
    return fetch('setup/kiosk/navigate/pending', { cache: 'no-store', signal: signal })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.pending) window.location.href = 'ui/?kiosk=1';
      })
      .catch(function () {});
  });
  setInterval(poll, 3000);
  poll();
})();
