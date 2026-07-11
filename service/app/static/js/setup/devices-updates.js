// The functions below are shared by the wizard AND the configured settings page
// (the Updates card, Diagnostics, and the satellite Sync Status panel), so they
// must render unconditionally. Gating them behind the wizard-only block left the
// settings page calling undefined functions, so the update buttons silently did
// nothing (Pantry Raider).
// Show an ISO-8601 UTC timestamp in the visitor's local time, e.g. for the
// satellite Sync Status panel. Returns the raw string if it cannot be parsed.
function fmtSyncTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  // Honor the 12/24-hour setting stamped on <html>; 'auto' keeps the browser
  // locale's own reading.
  const cf = document.documentElement.getAttribute('data-clock-format');
  if (cf === '12' || cf === '24') {
    return d.toLocaleString(undefined, { hour12: cf === '12' });
  }
  return d.toLocaleString();
}

// Render any sync-time stamps already on the page into local time.
function _initSyncTimes() {
  document.querySelectorAll('.sync-time[data-iso]').forEach((el) => {
    el.textContent = fmtSyncTime(el.getAttribute('data-iso'));
  });
}

// Redraw the Sync Status panel from a last_sync summary returned by the server.
function renderSyncStatus(ls) {
  const box = document.getElementById('sync-status');
  if (!box || !ls || !ls.at) return;
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const badge = ls.ok
    ? '<span class="badge bg-success me-2"><i class="bi bi-check-circle me-1"></i>Succeeded</span>'
    : '<span class="badge bg-danger me-2"><i class="bi bi-x-circle me-1"></i>Failed</span>';
  let body = '';
  if (ls.ok) {
    const applied = ls.applied || [];
    let line = 'Applied ' + applied.length + ' setting(s)';
    if (ls.defaults) line += ', ' + ls.defaults + ' expiry default(s)';
    body = '<div class="small mb-1">' + line + '.</div>';
    if (applied.length) {
      body += '<div class="small text-secondary">Fields: <code>' + esc(applied.join(', ')) + '</code></div>';
    }
  } else {
    body = '<div class="small text-danger">' + esc(ls.error || 'Sync failed.') + '</div>';
  }
  box.innerHTML =
    '<div class="d-flex align-items-center mb-2">' + badge +
    '<span class="text-secondary small">Last sync: <span class="sync-time">' +
    esc(fmtSyncTime(ls.at)) + '</span></span></div>' + body;
}

// Satellite: scan the LAN for a Pantry Raider server and fill the URL field.
async function scanLanForServer(inputId, resultId) {
  const inp = document.getElementById(inputId);
  const out = document.getElementById(resultId);
  if (!out) return;
  out.className = 'small mt-1 text-info';
  out.textContent = 'Scanning LAN...';
  try {
    const r = await fetch('/api/devices/scan', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
    const d = await r.json();
    if (!d.ok) {
      out.className = 'small mt-1 text-danger';
      out.textContent = 'Scan failed: ' + (d.error || 'unknown error');
      return;
    }
    // Only a full server (server / pi_hosted) can be a satellite's main server.
    // Exclude satellites, including THIS device, which reports mode 'pi_remote'
    // (FoodAssistant-52m7). The older 'satellite' value is filtered too for
    // safety on mixed-version LANs.
    const servers = (d.found || []).filter(h => h.mode !== 'pi_remote' && h.mode !== 'satellite');
    if (!servers.length) {
      out.className = 'small mt-1 text-warning';
      out.textContent = 'No Pantry Raider servers found on this subnet.';
      return;
    }
    if (servers.length === 1) {
      inp.value = 'http://' + servers[0].ip + ':' + servers[0].port;
      out.className = 'small mt-1 text-success';
      out.textContent = 'Found: ' + inp.value;
    } else {
      // Multiple results: a clickable list. Built with DOM APIs (textContent +
      // addEventListener), not string HTML, so a hostile LAN device that
      // advertises a crafted ip/port cannot inject markup or script into the
      // admin's setup page (security audit, Jul 2026).
      out.className = 'small mt-1';
      out.textContent = 'Found ' + servers.length + ' servers: ';
      servers.forEach(function(h) {
        var url = 'http://' + h.ip + ':' + h.port;
        var a = document.createElement('a');
        a.href = '#';
        a.className = 'me-2';
        a.textContent = url;
        a.addEventListener('click', function(ev) {
          ev.preventDefault();
          var el = document.getElementById(inputId);
          if (el) el.value = url;
          var res = document.getElementById(resultId);
          if (res) res.textContent = 'Selected: ' + url;
        });
        out.appendChild(a);
        out.appendChild(document.createTextNode(' '));
      });
    }
  } catch(e) {
    out.className = 'small mt-1 text-danger';
    out.textContent = 'Scan error: ' + e;
  }
}

// Satellite: save the upstream link, then pull backend config + defaults.
async function syncFromUpstream() {
  const out = document.getElementById('upstream-result');
  const now = document.getElementById('sync-now-result');
  for (const el of [out, now]) {
    if (el) { el.className = 'test-result text-secondary'; el.textContent = 'Syncing…'; }
  }
  try {
    const r = await fetch('setup/satellite/sync', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        remote_server_url: val('remote_server_url') || '',
        upstream_api_key: secretVal('upstream_api_key'),
      }),
    });
    const d = await r.json();
    const msg = d.ok ? d.message : (d.error || 'Sync failed.');
    for (const el of [out, now]) {
      if (el) {
        el.className = 'test-result ' + (d.ok ? 'text-success' : 'text-danger');
        el.textContent = msg;
      }
    }
    if (d.last_sync) renderSyncStatus(d.last_sync);
  } catch (e) {
    for (const el of [out, now]) {
      if (el) { el.className = 'test-result text-danger'; el.textContent = String(e); }
    }
  }
}

// One-image mode switch (FoodAssistant-dzx9): a Pi Hosted appliance becomes a
// satellite of another server (local stack paused, data kept), or switches back.
async function switchToSatellite(btn) {
  const out = document.getElementById('switch-satellite-result');
  const url = (val('switch_server_url') || '').trim();
  const key = (val('switch_upstream_api_key') || '').trim();
  if (!url) { out.className = 'test-result text-danger'; out.textContent = 'Enter the main server URL first.'; return; }
  if (!key) { out.className = 'test-result text-danger'; out.textContent = "Enter the main server's API key first."; return; }
  if (!confirm('Switch this appliance to satellite mode?\n\n' +
      'It will follow the server at ' + url + ' for inventory, recipes, and AI settings. ' +
      'The local Grocy and Mealie stop running, but every bit of their data stays on this device. ' +
      'You can switch back from this Settings page at any time.')) return;
  btn.disabled = true;
  out.className = 'test-result text-secondary';
  out.textContent = 'Checking the server, stopping the local stack, and syncing… this can take a minute.';
  try {
    const r = await fetch('setup/deployment/to-satellite', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({remote_server_url: url, upstream_api_key: key}),
    });
    const d = await r.json();
    out.className = 'test-result ' + (d.ok ? 'text-success' : 'text-danger');
    out.textContent = d.ok ? (d.message + ' Reloading…') : (d.error || 'The switch failed.');
    if (d.ok) setTimeout(() => location.reload(), 2500);
    else btn.disabled = false;
  } catch (e) {
    out.className = 'test-result text-danger';
    out.textContent = String(e);
    btn.disabled = false;
  }
}

async function switchToHosted(btn) {
  const out = document.getElementById('switch-hosted-result');
  if (!confirm('Switch this device back to running its own full stack?\n\n' +
      'The paused local Grocy and Mealie start again with all the data they had, ' +
      'this device stops following the main server, and its previous inventory, ' +
      'recipe, and AI settings are restored.')) return;
  btn.disabled = true;
  out.className = 'test-result text-secondary';
  out.textContent = 'Starting the local stack… this can take a minute.';
  try {
    const r = await fetch('setup/deployment/to-hosted', {method: 'POST'});
    const d = await r.json();
    out.className = 'test-result ' + (d.ok ? 'text-success' : 'text-danger');
    out.textContent = d.ok ? (d.message + ' Reloading…') : (d.error || 'The switch failed.');
    if (d.ok) setTimeout(() => location.reload(), 2500);
    else btn.disabled = false;
  } catch (e) {
    out.className = 'test-result text-danger';
    out.textContent = String(e);
    btn.disabled = false;
  }
}

// Updates: server manual "Update now" triggers Watchtower immediately so the
// user does not wait for the daily poll (Pantry Raider manual-server-update).
async function updateServerNow(btn) {
  const out = document.getElementById('update-result');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Updating...';
  if (out) { out.className = 'test-result text-secondary'; out.textContent = 'Asking the updater to pull the latest image...'; }
  try {
    const r = await fetch('setup/update-server', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      // Watchtower will recreate this container, dropping the connection, so
      // poll for the app to come back and report the new version.
      await _pollForRestart(out);
    } else if (out) {
      out.className = 'test-result text-danger';
      out.textContent = d.error || 'Update failed.';
    }
  } catch (e) {
    // The recreate dropped the connection before responding: poll for the app.
    await _pollForRestart(out);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Updates: persist the global auto-update flag (FoodAssistant-k2kk).
async function saveAutoUpdate(box) {
  const out = document.getElementById('auto-update-result');
  if (out) { out.className = 'test-result ms-1 text-secondary'; out.textContent = 'Saving...'; }
  try {
    const r = await fetch('setup/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_update: box.checked }),
    });
    const d = await r.json();
    if (out) {
      const ok = d.ok !== false;
      out.className = 'test-result ms-1 ' + (ok ? 'text-success' : 'text-danger');
      out.textContent = ok ? (box.checked ? 'Automatic updates on.' : 'Automatic updates off.') : 'Could not save.';
    }
  } catch (e) {
    if (out) { out.className = 'test-result ms-1 text-danger'; out.textContent = 'Could not save.'; }
  }
}

// Diagnostics: debug logging toggle + log download (FoodAssistant-asra).
async function _loadLoggingState() {
  const box = document.getElementById('debug_logging');
  if (!box) return;
  try {
    const d = await fetch('admin/logging', { cache: 'no-store' }).then(r => r.json());
    box.checked = !!d.enabled;
  } catch (e) { /* leave unchecked */ }
}

async function toggleDebugLogging(box) {
  const out = document.getElementById('logging-result');
  if (out) { out.className = 'test-result ms-2 text-secondary'; out.textContent = 'Saving...'; }
  try {
    const r = await fetch('admin/logging', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: box.checked }),
    });
    const d = await r.json();
    if (out) {
      out.className = 'test-result ms-2 ' + (d.ok ? 'text-success' : 'text-danger');
      out.textContent = d.ok ? (d.enabled ? 'Debug logging on.' : 'Debug logging off.') : 'Could not save.';
    }
  } catch (e) {
    if (out) { out.className = 'test-result ms-2 text-danger'; out.textContent = 'Could not save.'; }
  }
}

function downloadLogs() {
  // A plain navigation triggers the attachment download without fetch/blob.
  window.location = 'admin/logs/download';
}

// Satellite: passively detect whether a newer version exists, without touching
// the local git checkout. admin/check-update reads APP_VERSION from the main
// branch over HTTPS and compares it to this device's running version, so it
// reports availability even when the bridge OTA would report "already up to
// date" because of a stale local checkout (FoodAssistant-r7e6). The actual
// pull/restart stays on the separate "Update now" button.
async function checkSatelliteUpdate(btn) {
  // Always have somewhere to write: the availability line if present, otherwise
  // the result line, so the check never silently does nothing.
  const el = document.getElementById('update-avail') || document.getElementById('update-result');
  let orig;
  if (btn) { orig = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking…'; }
  const write = function (cls, html) { if (el) el.innerHTML = '<span class="' + cls + '">' + html + '</span>'; };
  write('text-secondary', '<i class="bi bi-hourglass-split me-1"></i>Checking for a newer version…');
  try {
    const r = await fetch('admin/check-update', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (!d.ok) {
      write('text-secondary', '<i class="bi bi-dash-circle me-1"></i>' +
        (d.error || 'Update check unavailable') + ' (running v' + (d.current || '?') + ').');
    } else if (d.update_available) {
      write('text-warning', '<i class="bi bi-exclamation-circle-fill me-1"></i>' +
        'Update available: <strong>' + d.latest + '</strong> (you have v' + d.current + '). Press Update now to apply it.');
    } else {
      write('text-success', '<i class="bi bi-check-circle-fill me-1"></i>' +
        'You are on the latest version (v' + d.current + ').');
    }
    // Refresh the "last checked" line (a full timestamp in the configured zone
    // renders on the next page load; "just now" is accurate until then).
    const lc = document.getElementById('update-last-checked');
    if (lc && d.ok) {
      lc.innerHTML = 'Last checked: just now' + (d.update_available
        ? ' <span class="text-warning">(v' + d.latest + ' available)</span>'
        : ' (up to date)');
    }
  } catch (e) {
    console.error('check-update failed:', e);
    write('text-danger', '<i class="bi bi-x-circle me-1"></i>Update check failed (' + (e && e.message ? e.message : e) + '). The server may not be able to reach GitHub.');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Date & time + Maintenance (FoodAssistant-lq01/-amp0/-wvwm).
async function saveTimezone(btn) {
  const el = document.getElementById('timezone-result');
  const tz = document.getElementById('timezone')?.value ?? '';
  if (el) el.innerHTML = '<span class="text-secondary">Saving…</span>';
  if (btn) btn.disabled = true;
  // The clock format select sits in the same Date & time card and saves with
  // the same button; absent (older markup) it is simply left out of the POST.
  const body = { timezone: tz };
  const cf = document.getElementById('clock_format');
  if (cf) body.clock_format = cf.value;
  try {
    const r = await fetch('setup/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json();
    if (d.ok) { if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved.</span>'; setTimeout(() => location.reload(), 600); }
    else if (el) el.innerHTML = '<span class="text-danger">' + (d.error || 'Failed') + '</span>';
  } catch (e) { if (el) el.innerHTML = '<span class="text-danger">' + e + '</span>'; }
  finally { if (btn) btn.disabled = false; }
}

async function reloadSettings(btn) {
  const el = document.getElementById('maintenance-result');
  if (el) el.innerHTML = '<span class="text-secondary">Reloading…</span>';
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('setup/maintenance/reload', { method: 'POST' });
    const d = await r.json();
    if (el) el.innerHTML = d.ok ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Settings reloaded.</span>'
      : '<span class="text-danger">' + (d.error || 'Failed') + '</span>';
  } catch (e) { if (el) el.innerHTML = '<span class="text-danger">' + e + '</span>'; }
  finally { if (btn) btn.disabled = false; }
}

async function rebootNow(btn) {
  if (!confirm('Reboot this appliance now?')) return;
  const el = document.getElementById('maintenance-result');
  if (el) el.innerHTML = '<span class="text-secondary">Rebooting…</span>';
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('setup/maintenance/reboot', { method: 'POST' });
    const d = await r.json();
    if (el) el.innerHTML = d.ok ? '<span class="text-warning"><i class="bi bi-power me-1"></i>Rebooting. The device will be back shortly.</span>'
      : '<span class="text-danger">' + (d.error || 'Failed') + '</span>';
  } catch (e) { if (el) el.innerHTML = '<span class="text-warning">Reboot requested (the connection dropped, as expected).</span>'; }
}

// Show the day picker only for a weekly reboot, and the time only when the
// schedule is on at all (FoodAssistant-8x4u).
function rebootFreqChanged() {
  const f = document.getElementById('scheduled_reboot_frequency')?.value || 'off';
  document.getElementById('scheduled-reboot-day-wrap')?.classList.toggle('d-none', f !== 'weekly');
  document.getElementById('scheduled-reboot-time-wrap')?.classList.toggle('d-none', f === 'off');
}

async function saveScheduledReboot(btn) {
  const el = document.getElementById('scheduled-reboot-result');
  const f = document.getElementById('scheduled_reboot_frequency')?.value || 'off';
  const day = parseInt(document.getElementById('scheduled_reboot_day')?.value ?? '0', 10) || 0;
  const t = document.getElementById('scheduled_reboot_time')?.value ?? '';
  if (f !== 'off' && !t) {
    if (el) el.innerHTML = '<span class="text-danger">Pick a reboot time first.</span>';
    return;
  }
  if (el) el.innerHTML = '<span class="text-secondary">Saving…</span>';
  if (btn) btn.disabled = true;
  try {
    // Turning the schedule off keeps the stored time, so switching it back
    // on later starts from the previous choice.
    const body = { scheduled_reboot_frequency: f, scheduled_reboot_day: day };
    if (f !== 'off') body.scheduled_reboot_time = t;
    const r = await fetch('setup/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json();
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const msg = f === 'off' ? 'Scheduled reboot disabled.'
      : f === 'weekly' ? 'Weekly reboot set for ' + days[day] + ' at ' + t + '.'
      : 'Nightly reboot set for ' + t + '.';
    if (el) el.innerHTML = d.ok
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>' + msg + '</span>'
      : '<span class="text-danger">' + (d.error || 'Failed') + '</span>';
  } catch (e) { if (el) el.innerHTML = '<span class="text-danger">' + e + '</span>'; }
  finally { if (btn) btn.disabled = false; }
}

// Satellite: pull the latest source and restart the app via the host bridge.
// The request can take a couple of minutes when dependencies change, so disable
// the button and show progress. The result includes a short before/after commit
// summary and the full update log.
// An in-app update restarts the app (Pi bridge OTA) or recreates this container
// (server Watchtower), so the request connection drops mid-flight, which the
// browser reports as "Failed to fetch" / could not reach the server. That is
// expected and usually means the update is underway, so instead of showing an
// error we poll /health until the app is back and report the new version.
async function _pollForRestart(outEl) {
  const before = window.__APP_VERSION || '';
  if (outEl) {
    outEl.className = 'test-result text-secondary';
    outEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Update in progress. The app is restarting, waiting for it to come back...';
  }
  const deadline = Date.now() + 240000;  // up to 4 minutes
  await new Promise(r => setTimeout(r, 5000));  // let the old process drop first
  while (Date.now() < deadline) {
    try {
      const r = await fetch('health', { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        const v = d.version || '';
        if (v && v !== before) {
          if (outEl) { outEl.className = 'test-result text-success'; outEl.textContent = 'Updated to v' + v + '. Reloading...'; }
          setTimeout(() => location.reload(), 1500);
          return;
        }
        if (v) {
          if (outEl) {
            outEl.className = 'test-result text-secondary';
            // Same version back can mean already current, OR a just-released
            // update whose image is still building. Say both plainly so the
            // user waits and retries instead of thinking the update failed.
            outEl.textContent = 'You are on v' + v + ', the newest this device could pull. '
              + 'If a new version was just released, its image can take a few minutes to finish building; '
              + 'wait a moment, then try Update again.';
          }
          return;
        }
      }
    } catch (e) { /* still restarting; keep waiting */ }
    await new Promise(r => setTimeout(r, 3000));
  }
  if (outEl) { outEl.className = 'test-result text-warning'; outEl.textContent = 'The app did not come back within a few minutes. Check the device and reload the page.'; }
}

async function checkForUpdates() {
  const out = document.getElementById('update-result');
  const logEl = document.getElementById('update-log');
  const btn = document.getElementById('update-btn');
  // Persistent, obvious progress state so the user does not press Update again
  // or leave the page during a slow Pi Remote update (Pantry Raider).
  if (out) {
    out.className = 'test-result text-info';
    out.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>'
      + '<strong>Updating…</strong> This can take a few minutes on a Pi Remote. '
      + 'Please wait, do not press Update again, and keep this page open.';
  }
  if (logEl) { logEl.classList.add('d-none'); logEl.textContent = ''; }
  const _btnOrig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Updating…'; }
  try {
    const r = await fetch('setup/update', { method: 'POST' });
    const d = await r.json();
    if (logEl && d.log) { logEl.textContent = d.log; logEl.classList.remove('d-none'); }
    if (d.ok && d.before && d.after && d.before !== d.after) {
      // Updated: the app restarted, so poll for it to come back and reload.
      await _pollForRestart(out);
      return;
    }
    if (out) {
      out.className = 'test-result ' + (d.ok ? 'text-success' : 'text-danger');
      if (d.ok) {
        out.textContent = (d.before && d.after && d.before === d.after)
          ? 'Already up to date (' + d.after + ').'
          : 'Updated' + (d.after ? ' to ' + d.after : '') + '.';
      } else {
        out.textContent = d.error || 'Update failed.';
      }
    }
  } catch (e) {
    // The connection dropped, almost always because the update restarted the
    // app. Poll for it to come back rather than reporting a fetch error.
    await _pollForRestart(out);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = _btnOrig; }
  }
}


// Updates: persist the fleet update channel (FoodAssistant-wkwx).
async function saveUpdateChannel(sel) {
  const out = document.getElementById('update-channel-result');
  if (out) { out.className = 'test-result ms-1 text-secondary'; out.textContent = 'Saving...'; }
  try {
    const r = await fetch('setup/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ update_channel: sel.value }),
    });
    const d = await r.json();
    if (out) {
      const ok = d.ok !== false;
      out.className = 'test-result ms-1 ' + (ok ? 'text-success' : 'text-danger');
      out.textContent = ok
        ? (sel.value === 'stable' ? 'Following releases only.' : 'Following every change.')
        : 'Could not save.';
    }
  } catch (e) {
    if (out) { out.className = 'test-result ms-1 text-danger'; out.textContent = 'Could not save.'; }
  }
}

function downloadSupportBundle() {
  // Plain navigation; on a Pi the server also asks the host bridge for its
  // report, so the zip can take a few seconds to start downloading.
  const out = document.getElementById('logging-result');
  if (out) {
    out.className = 'test-result ms-2 text-secondary';
    out.textContent = 'Building the bundle, the download starts in a moment...';
    setTimeout(() => { if (out.textContent.startsWith('Building')) out.textContent = ''; }, 12000);
  }
  window.location = 'admin/support-bundle';
}
