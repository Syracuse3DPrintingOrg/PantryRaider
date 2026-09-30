// Kiosk display wake reporter (FoodAssistant-otiy).
//
// Runs only in kiosk mode. The host bridge owns the display idle timeout and
// blanking loop; this script's job is to tell the bridge when the user is
// active so it (a) does not blank while the screen is in use and (b) wakes the
// display the instant the user touches it again. Activity here also wakes the
// Stream Deck, which polls the same bridge state, so a touch on the screen and
// a press on the deck each wake both surfaces while their timeouts stay
// independent.
//
// The reporter is leading-edge throttled: the first event after a quiet period
// fires immediately (so a touch on a blanked screen wakes it right away),
// then at most once per THROTTLE_MS while activity continues.
(function () {
  if (localStorage.getItem('kioskMode') !== 'true') return;

  var THROTTLE_MS = 5000;
  var lastSent = 0;
  var pending = false;

  function report() {
    fetch('setup/kiosk/activity', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      keepalive: true,
      cache: 'no-store',
    }).catch(function () { });
  }

  function onActivity() {
    var now = Date.now();
    if (now - lastSent >= THROTTLE_MS) {
      lastSent = now;
      report();
    } else if (!pending) {
      // Coalesce a trailing report so a burst still refreshes the timer.
      pending = true;
      setTimeout(function () {
        pending = false;
        lastSent = Date.now();
        report();
      }, THROTTLE_MS - (now - lastSent));
    }
  }

  // 'mousemove' is deliberately NOT in this list (FoodAssistant-ofip). A kiosk
  // is a touch panel: real interaction fires pointerdown/touchstart/click, and
  // a rare attached mouse still fires click/wheel/keydown. Meanwhile the vc4
  // HDMI CEC input masquerades as a pointer and can emit stray movement events
  // even with the compositor cursor hidden; treating those as activity woke the
  // Stream Deck for 1-3 seconds on its own. Dropping mousemove stops the phantom
  // wakes at the browser, alongside the udev LIBINPUT_IGNORE_DEVICE rule, without
  // losing any real wake signal.
  var events = ['pointerdown', 'touchstart', 'keydown', 'wheel', 'click'];
  for (var i = 0; i < events.length; i++) {
    window.addEventListener(events[i], onActivity, { passive: true });
  }

  // The screensaver swallows the tap that dismisses it (capture phase plus
  // stopPropagation, so the tap cannot press whatever sits under the overlay).
  // That tap never reaches the listeners above, so waking the screensaver by
  // touch reported no activity and the panel's sleep countdown kept running
  // from before the screensaver appeared: the screen went dark seconds after
  // someone woke it (FoodAssistant-9k2v). The screensaver calls this instead.
  window.__prKioskActivity = onActivity;

  // Report once on load so a fresh kiosk page counts as activity.
  onActivity();
})();
