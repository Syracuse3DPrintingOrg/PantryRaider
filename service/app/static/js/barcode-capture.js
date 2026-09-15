// Global barcode capture, for every page except Add (which has its own).
// Formerly an inline block at the end of base.html, emitted only when the
// barcode_global_capture setting is on and the page is not Add; the same
// template condition now wraps the <script src> that loads this file, so it is
// still
// only present under exactly those conditions, just cached between pages.

// Global barcode capture (any page except Add, which has its own handler).
// A USB/Bluetooth HID scanner types the code as a fast keystroke burst. When
// one is seen here, save it to the pending list and jump to the Manage Pantry
// page. Off when the "Scan from any page" setting is disabled. Only a rapid
// burst counts, and never while a field is focused, so typing is untouched.
(function () {
  // Inter-keystroke gap that starts a fresh scan. A USB HID scanner types a
  // burst, but on a slower box (a Pi Remote) keystrokes can jitter, and a
  // tight gap split a single scan so only the tail survived ("035483" is the
  // last 6 digits of "078000035483"), FoodAssistant-pmry. A generous window
  // tolerates that jitter while still being faster than human typing, and
  // Enter remains the real terminator, so genuine scans are not merged.
  var buf = '', last = 0, GAP_MS = 300;
  function submit(code) {
    fetch('pending/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ barcode: code, quantity: 1, source: 'scanner' }),
    }).then(function () {
      // Land on Manage Pantry, where the saved-to-Pending count is shown.
      window.location.href = 'ui/add';
    }).catch(function () {});
  }
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    var ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) return;
    var now = Date.now();
    if (now - last > GAP_MS) buf = '';
    last = now;
    if (e.key === 'Enter') {
      if (buf.length >= 6) { var c = buf; buf = ''; submit(c); e.preventDefault(); }
      buf = '';
      return;
    }
    if (e.key.length === 1) buf += e.key;
  });
})();
