// Shared page behaviour for every base.html page: the kiosk-mode toggle, the
// inbox badges, the system-health indicator, and the calibration and setup
// hand-off pollers.
//
// These were six inline <script> blocks at the end of base.html, re-parsed and
// re-compiled on every navigation because inline scripts cannot be cached.
// They are here verbatim and in the same order, loaded from the same place in
// the document (after bootstrap, before the page's own scripts block),
// synchronously and
// NOT deferred, so nothing about when they run or what they can see changed.
// Classic scripts share one global scope whether they are one tag or six, so
// joining them changes nothing either; toggleKioskMode() stays a global
// because base.html's onclick calls it. The seventh block, global barcode
// capture, is conditional on a setting and the current page, so it lives in
// barcode-capture.js behind the same template condition in base.html.
//
// The blocks that read PRKioskStatus do so inside DOMContentLoaded handlers,
// which fire after the deferred kiosk-status.js has run; that is the rule
// tests/test_deferred_script_order.py enforces for every static script.

// CSS zoom on <html> can confuse Bootstrap's backdrop cleanup. Force-remove
// any orphaned backdrops and body classes when any modal closes.
document.addEventListener('hidden.bs.modal', function () {
  document.querySelectorAll('.modal-backdrop').forEach(function (el) { el.remove(); });
  document.body.classList.remove('modal-open');
  document.body.style.removeProperty('overflow');
  document.body.style.removeProperty('padding-right');
});

// ---------------------------------------------------------------------------

// Kiosk mode toggle
(function() {
  var active = localStorage.getItem('kioskMode') === 'true';
  var icon = document.getElementById('kioskIcon');
  if (active && icon) {
    icon.className = 'bi bi-tablet-fill';
    document.getElementById('kioskToggle').title = 'Kiosk mode ON, click to disable';
  }
})();
function toggleKioskMode() {
  var active = localStorage.getItem('kioskMode') === 'true';
  // Turning kiosk mode ON hides the mouse cursor on this screen (kiosk.css
  // sets cursor:none), which surprises anyone who flips it on from a normal
  // browser. Warn at the moment of enabling and say how to get the cursor
  // back. Only prompt on enable, never on disable, and never on the Pi's
  // own auto-enable path (kiosk-auto.js latches the flag directly and does
  // not call this), so it never nags on every page.
  if (!active) {
    var ok = window.confirm(
      'Kiosk mode hides the mouse cursor on this screen and sizes everything for touch. '
      + 'To bring the cursor back, click the tablet icon in the top bar again to turn kiosk mode off.\n\n'
      + 'Turn kiosk mode on now?');
    if (!ok) { return; }
  }
  localStorage.setItem('kioskMode', active ? 'false' : 'true');
  // Mark this as an explicit user choice so the Pi display auto-enable
  // (kiosk-auto.js, FoodAssistant-h437) never overrides it in either
  // direction, and drop the auto marker since the user is now in control.
  localStorage.setItem('kioskExplicit', 'true');
  localStorage.removeItem('kioskAuto');
  location.reload();
}

// ---------------------------------------------------------------------------

// Inbox badges in the navbar: pending scans (yellow) + action items (red).
// Prefer the consolidated kiosk poll so the badges refresh live too; fall
// back to a one-shot count fetch on a page without the shared loop. Waits
// for DOMContentLoaded because kiosk-status.js is deferred, so
// window.PRKioskStatus only exists once the deferred scripts have run
// (FoodAssistant-0hwez).
document.addEventListener('DOMContentLoaded', function () {
  function setBadge(id, count) {
    var b = document.getElementById(id);
    if (!b) return;
    if (count > 0) { b.textContent = count; b.classList.remove('d-none'); }
    else { b.classList.add('d-none'); }
  }
  if (window.PRKioskStatus) {
    window.PRKioskStatus.subscribe(function (s) {
      if (!s || !s.counts) return;
      if (typeof s.counts.pending === 'number') setBadge('pending-nav-badge', s.counts.pending);
      if (typeof s.counts.actions === 'number') setBadge('actions-nav-badge', s.counts.actions);
    }, { interval: 30000 });
  } else {
    fetch('pending/count').then(r => r.json()).then(d => setBadge('pending-nav-badge', d.count)).catch(() => {});
    fetch('action-items/count').then(r => r.json()).then(d => setBadge('actions-nav-badge', d.count)).catch(() => {});
  }
});

// ---------------------------------------------------------------------------

// System-health indicator. Polls the host bridge (via the app) for power,
// thermal, and disk warnings on a Pi appliance. Off a Pi the endpoint
// returns an empty warnings list, so the icon stays hidden everywhere
// else. Waits for DOMContentLoaded so the deferred kiosk-status.js has
// defined window.PRKioskStatus by the time it is looked for.
document.addEventListener('DOMContentLoaded', function () {
  var item = document.getElementById('sysHealthItem');
  var link = document.getElementById('sysHealthLink');
  if (!item || !link) return;
  function applyHealth(d) {
    var warnings = (d && d.warnings) || [];
    if (warnings.length) {
      link.setAttribute('title', warnings.map(function (w) { return w.message; }).join('\n'));
      item.classList.remove('d-none');
    } else {
      item.classList.add('d-none');
    }
  }
  function refresh() {
    fetch('setup/system/health', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(applyHealth)
      .catch(function () { /* leave the icon as-is on a transient error */ });
  }
  // Prefer the consolidated kiosk poll (the server caches the bridge health
  // at the bridge's own ~60s rhythm, so a fast client poll stays cheap);
  // fall back to the dedicated 60s health poll without the shared loop. The
  // health field is admin-gated, so on a non-admin viewer it is omitted and
  // the icon just keeps its state, exactly as a 403 leaves it today.
  if (window.PRKioskStatus) {
    window.PRKioskStatus.subscribe(function (s) { if (s && s.health) applyHealth(s.health); }, { interval: 60000 });
  } else {
    refresh();
    // Re-check every minute so the icon is not badly stale when a power or
    // heat condition comes and goes; the on-screen toast (FoodAssistant-h28s)
    // is the primary alert, this keeps the nav triangle roughly in step.
    setInterval(function () { if (!document.hidden) refresh(); }, 60000);
  }
});

// ---------------------------------------------------------------------------

// Touch-calibration launcher (kiosk only). A "Calibrate" click in any
// browser sets a server flag; the kiosk on the Pi polls for it and drives
// its own display to the fullscreen calibration page, so the person taps
// the physical screen with the crosshair visible in front of them. Waits
// for DOMContentLoaded so the deferred kiosk-status.js has defined
// window.PRKioskStatus by the time it is looked for.
document.addEventListener('DOMContentLoaded', function () {
  if (localStorage.getItem('kioskMode') !== 'true') return;
  function go(pending) { if (pending) window.location.href = '../setup/calibrate/touch/page'; }
  function poll() {
    // The physical kiosk display is never hidden; this only spares a
    // backgrounded browser tab that happens to have kiosk mode latched.
    if (document.hidden) return;
    fetch('../setup/calibrate/touch/pending', { cache: 'no-store' })
      .then(r => r.json())
      .then(d => go(d && d.pending))
      .catch(() => {});
  }
  // Prefer the consolidated kiosk poll; fall back to the dedicated pending
  // poll without the shared loop.
  if (window.PRKioskStatus) {
    window.PRKioskStatus.subscribe(function (s) { if (s && s.calibrate_pending) go(true); }, { interval: 10000 });
  } else {
    setInterval(poll, 10000);
    poll();
  }
});

// ---------------------------------------------------------------------------

// Setup-complete kiosk hand-off (kiosk only). The wizard is finished from a
// remote browser, so the Pi's attached display is left on the wizard page.
// The save that completes setup raises a server flag (FoodAssistant-6v9q);
// the kiosk polls for it and drives its own display to the dashboard. The
// flag is one-shot (cleared when served), so this fires once and does not
// loop. Re-latch kiosk mode via ?kiosk=1, like the calibration page does
// on its way back. The setup page runs its own copy of this poll, since it
// does not extend this template. Waits for DOMContentLoaded so the
// deferred kiosk-status.js has defined window.PRKioskStatus by the time
// it is looked for.
document.addEventListener('DOMContentLoaded', function () {
  if (localStorage.getItem('kioskMode') !== 'true') return;
  function go(pending) { if (pending) window.location.href = '../ui/?kiosk=1'; }
  function poll() {
    if (document.hidden) return;
    fetch('../setup/kiosk/navigate/pending', { cache: 'no-store' })
      .then(r => r.json())
      .then(d => go(d && d.pending))
      .catch(() => {});
  }
  // Prefer the consolidated kiosk poll: it carries nav_pending only for a
  // kiosk-latched caller (kiosk=1), and consuming it there clears the
  // one-shot flag once, so folding both old pollers (this and the setup
  // page's copy) into one per page removes the old double-consume race.
  // Fall back to the dedicated pending poll without the shared loop.
  if (window.PRKioskStatus) {
    window.PRKioskStatus.subscribe(function (s) { if (s && s.nav_pending) go(true); }, { interval: 10000 });
  } else {
    setInterval(poll, 10000);
    poll();
  }
});
