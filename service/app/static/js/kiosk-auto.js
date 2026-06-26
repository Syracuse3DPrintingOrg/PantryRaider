/*
 * Auto-enable kiosk mode on a Pi with an attached display (FoodAssistant-h437).
 *
 * On a Raspberry Pi appliance with a physical screen connected, kiosk (touch)
 * mode should default ON without the user having to toggle it, while still
 * RESPECTING an explicit user choice in either direction. This runs only when:
 *   - the page reports data-is-pi="1" on <html> (server-side is_pi flag), and
 *   - the user has NOT made an explicit choice (no kioskExplicit marker), and
 *   - kiosk mode is not already on.
 * It then probes the host bridge once (via the app at setup/hardware/status)
 * and, if a display is present, latches kioskMode plus a separate kioskAuto
 * marker so a later run can tell auto-enable from an explicit user toggle.
 *
 * Markers in localStorage:
 *   kioskMode     'true'/'false' — the latched mode kiosk.css / scripts read.
 *   kioskExplicit 'true'         — set by the nav toggle; auto-enable defers to it.
 *   kioskAuto     'true'         — set here, so this was an auto-enable not a toggle.
 *
 * Off-Pi and when the endpoint is unreachable this is a no-op: it never enables
 * kiosk mode anywhere but a Pi with a confirmed display, and never overrides an
 * explicit user 'off'.
 */
(function () {
  try {
    var html = document.documentElement;
    if (!html || html.getAttribute('data-is-pi') !== '1') return; // off-Pi: do nothing

    // Respect an explicit user choice (toggle button sets kioskExplicit).
    if (localStorage.getItem('kioskExplicit') === 'true') return;

    // Already in kiosk mode (e.g. ?kiosk=1 latch or a prior auto-enable): nothing to do.
    if (localStorage.getItem('kioskMode') === 'true') return;
  } catch (e) {
    return; // no storage / private mode: stay a no-op
  }

  // Probe the host bridge once for display presence. The app proxies this at
  // setup/hardware/status and returns { display: { present: bool, ... } }.
  fetch('setup/hardware/status', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var present = d && d.display && d.display.present;
      if (!present) return; // no display attached: leave kiosk mode off
      try {
        // Auto-enable, marking it as automatic so an explicit toggle can win later.
        localStorage.setItem('kioskMode', 'true');
        localStorage.setItem('kioskAuto', 'true');
      } catch (e) { return; }
      // Reload so kiosk.css loads before paint (the inline bootstrap in base.html
      // injects it from the kioskMode flag on the next load).
      location.reload();
    })
    .catch(function () { /* unreachable bridge: no-op */ });
})();
