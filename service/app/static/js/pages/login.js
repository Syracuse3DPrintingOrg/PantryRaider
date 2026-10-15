// Sign-in page (templates/login.html).
//
// Kitchen screen self-heal: a screen on this device that finds itself
// signed out asks the device to start it again, which signs it in with a
// fresh launch ticket. At most once every 10 minutes; the password form
// stays usable either way.

(function () {
  var on = false, last = 0;
  try {
    on = localStorage.getItem('kioskMode') === 'true';
    last = parseInt(localStorage.getItem('prKioskRelaunchAt') || '0', 10) || 0;
  } catch (e) { return; }
  if (!on) return;
  // This device's own address: localhost, a loopback address, or the
  // device's bare host name (a screen can open http://pr:9284). The
  // device only restarts a screen that asks from itself anyway.
  var h = location.hostname;
  var local = h === '127.0.0.1' || h === '[::1]' ||
    /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/i.test(h);
  if (!local) return;
  var now = Date.now();
  if (now - last < 10 * 60 * 1000) return;
  try { localStorage.setItem('prKioskRelaunchAt', String(now)); } catch (e) { return; }
  fetch('ui/kiosk/relaunch', {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: '{}', credentials: 'same-origin'})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.restarting) {
        var el = document.getElementById('kiosk-relaunch-note');
        if (el) el.classList.remove('d-none');
      }
    })
    .catch(function () {});
})();
