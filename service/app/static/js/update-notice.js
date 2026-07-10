/*
 * On-screen "update available" popup (Pantry Raider, FoodAssistant-5wtc).
 *
 * On a normal page load this asks admin/update-notice whether a newer version
 * is out. When one is, and this browser has not already dismissed that exact
 * version, it shows a single small banner with two actions:
 *   - Update : on a Pi appliance it triggers the in-app OTA update
 *              (POST setup/update, the same trigger the Settings page uses);
 *              anywhere else it opens the release page in a new tab.
 *   - Dismiss: hides the banner and remembers the version so it never pops
 *              again for that version. A later, newer version pops once more.
 *
 * Two guards keep the check cheap and quiet: the client only checks at most
 * once every few hours per browser (a localStorage timestamp), and the server
 * side prefers its own recent result, so GitHub is not hammered even by the
 * kiosk's frequent reloads. Any failed or throttled check is a silent no-op.
 */
(function () {
  var cfg = document.getElementById('update-notice-config');
  if (!cfg) return;

  var THROTTLE_MS = 3 * 60 * 60 * 1000;   // at most one check per 3h per browser
  var LAST_KEY = 'updateNoticeLastCheck';
  var DISMISS_KEY = 'updateNoticeDismissed';

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { } }

  // Client-side throttle: skip entirely if we checked recently.
  var last = parseInt(lsGet(LAST_KEY) || '0', 10);
  if (last && (Date.now() - last) < THROTTLE_MS) return;

  var dismissed = lsGet(DISMISS_KEY) || '';
  var url = 'admin/update-notice?dismissed=' + encodeURIComponent(dismissed);

  fetch(url, { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      // Only record the check once the server actually answered, so a transient
      // network error does not suppress the next page load's attempt.
      if (d) lsSet(LAST_KEY, String(Date.now()));
      if (!d || !d.ok || !d.show || !d.latest) return;
      showPopup(d);
    })
    .catch(function () { /* offline or blocked: stay silent */ });

  // --- styles + banner ------------------------------------------------------
  function injectStyles() {
    if (document.getElementById('update-notice-style')) return;
    var style = document.createElement('style');
    style.id = 'update-notice-style';
    style.textContent =
      '.upd-notice{position:fixed;left:50%;bottom:18px;transform:translateX(-50%) translateY(16px);' +
      'z-index:2055;max-width:min(92vw,420px);background:#1f242b;color:#e6e9ed;' +
      'border:1px solid #333b45;border-left:5px solid #F2006E;border-radius:10px;' +
      'box-shadow:0 .5rem 1.2rem rgba(0,0,0,.45);padding:12px 14px;opacity:0;' +
      'transition:opacity .25s,transform .25s}' +
      '.upd-notice.show{opacity:1;transform:translateX(-50%) translateY(0)}' +
      '.upd-notice .upd-title{font-weight:700;margin-bottom:2px}' +
      '.upd-notice .upd-msg{font-size:.92rem;color:#c7ccd3;margin-bottom:10px}' +
      '.upd-notice .upd-actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}' +
      '.upd-notice button{border:0;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:.9rem}' +
      '.upd-notice .upd-update{background:#F2006E;color:#fff}' +
      '.upd-notice .upd-update:hover{background:#d0005f}' +
      '.upd-notice .upd-dismiss{background:transparent;color:#9aa4af}' +
      '.upd-notice .upd-dismiss:hover{color:#e6e9ed}' +
      '.upd-notice .upd-status{font-size:.85rem;color:#c7ccd3}';
    document.head.appendChild(style);
  }

  function remember(version) { lsSet(DISMISS_KEY, version); }

  function showPopup(d) {
    injectStyles();
    var box = document.createElement('div');
    box.className = 'upd-notice';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', 'Update available');
    box.innerHTML =
      '<div class="upd-title">Update available</div>' +
      '<div class="upd-msg"></div>' +
      '<div class="upd-actions">' +
      '<button type="button" class="upd-dismiss">Dismiss</button>' +
      '<button type="button" class="upd-update"></button>' +
      '</div>';
    box.querySelector('.upd-msg').textContent =
      'Pantry Raider ' + d.latest + ' is ready. You are on ' + (d.current || '?') + '.';
    var updateBtn = box.querySelector('.upd-update');
    updateBtn.textContent = d.is_pi_appliance ? 'Update now' : 'View update';

    box.querySelector('.upd-dismiss').onclick = function () {
      remember(d.latest);
      remove(box);
    };
    updateBtn.onclick = function () { doUpdate(d, box); };

    document.body.appendChild(box);
    requestAnimationFrame(function () { box.classList.add('show'); });
  }

  function remove(box) {
    box.classList.remove('show');
    setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 260);
  }

  function status(box, text) {
    var actions = box.querySelector('.upd-actions');
    if (actions) actions.remove();
    var msg = box.querySelector('.upd-msg');
    if (msg) { msg.className = 'upd-status'; msg.textContent = text; }
  }

  function doUpdate(d, box) {
    if (!d.is_pi_appliance) {
      // No in-app OTA here (a server or a remote browser): send the user to the
      // release page and treat that as handled so it does not keep nagging.
      try { window.open(d.release_url, '_blank', 'noopener'); } catch (e) { }
      remember(d.latest);
      remove(box);
      return;
    }
    // Pi appliance: trigger the same OTA the Settings "Update now" button uses.
    // The updater itself lives in the host bridge; we only kick the trigger and
    // then wait for the service to restart on the new version.
    status(box, 'Updating. This can take a few minutes and the app will restart. Please keep this page open.');
    fetch('setup/update', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.ok && res.before && res.after && res.before !== res.after) {
          pollForRestart(box);
        } else if (res && res.ok) {
          status(box, 'Already up to date (' + (res.after || d.current) + ').');
          setTimeout(function () { remove(box); }, 4000);
        } else {
          status(box, (res && res.error) || 'Update could not start. Open Settings to try again.');
        }
      })
      .catch(function () {
        // The connection usually drops because the update restarted the app.
        pollForRestart(box);
      });
  }

  function pollForRestart(box) {
    var tries = 0;
    (function ping() {
      tries += 1;
      fetch('admin/version', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function () { window.location.reload(); })
        .catch(function () {
          if (tries < 90) setTimeout(ping, 3000);
          else status(box, 'The update is taking a while. Reload the page to check on it.');
        });
    })();
  }
})();
