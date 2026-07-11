function _renderHwRow(name, st) {
  const row = document.getElementById(name + '-status-row');
  const btn = document.getElementById('btn-install-' + name);
  const showBtn = (show) => { if (btn) btn.classList.toggle('d-none', !show); };
  if (!row) return;
  if (!st) { row.innerHTML = '<span class="text-secondary small">Status unavailable.</span>'; showBtn(false); return; }
  const hw = name === 'kiosk' ? 'Display' : 'Stream Deck';
  if (st.active) {
    row.innerHTML = '<span class="text-success small"><i class="bi bi-check-circle-fill me-1"></i>Service installed and running.</span>';
    showBtn(false);
  } else if (st.installing) {
    row.innerHTML = '<span class="text-info small"><span class="spinner-border spinner-border-sm me-1"></span>Installing, this can take a few minutes...</span>';
    showBtn(false);
  } else if (st.installed && st.present) {
    // Installed but not running with hardware attached. The kiosk starts on
    // its own (the appliance watches for a connected display); the Stream
    // Deck pane has its own Restart service button.
    row.innerHTML = '<span class="text-warning small"><i class="bi bi-exclamation-circle me-1"></i>Service installed but not running.' +
      (name === 'kiosk'
        ? ' It starts on its own within a minute of a display being connected; reboot the device if it does not.'
        : ' Use Restart service below, or reboot the device.') + '</span>';
    showBtn(false);
  } else if (st.installed) {
    row.innerHTML = '<span class="text-secondary small"><i class="bi bi-dash-circle me-1"></i>Service installed. No ' +
      (name === 'kiosk' ? 'display' : 'Stream Deck') + ' detected right now; it starts when one is attached.</span>';
    showBtn(false);
  } else if (st.present) {
    row.innerHTML = '<span class="text-warning small"><i class="bi bi-exclamation-circle me-1"></i>' + hw + ' detected, but the service is not set up.</span>';
    showBtn(true);
  } else {
    row.innerHTML = '<span class="text-secondary small"><i class="bi bi-dash-circle me-1"></i>No hardware detected. Plug it in, then refresh.</span>';
    showBtn(false);
  }
}

async function loadHardwareStatus() {
  try {
    const r = await fetch('setup/hardware/status').then(x => x.json());
    if (!r.ok) { _renderHwRow('kiosk', null); _renderHwRow('streamdeck', null); return; }
    _renderHwRow('kiosk', r.kiosk);
    _renderHwRow('streamdeck', r.streamdeck);

    // Auto-select model when the bridge can identify the attached deck.
    _applyDeckAutodetect(r.streamdeck);

    // Load streamdeck config
    try {
      const cfg_resp = await fetch('setup/streamdeck/config').then(x => x.json());
      if (cfg_resp.ok && cfg_resp.config) {
        const cfg = cfg_resp.config;
        if (cfg.rotation !== undefined) {
          const rotEl = document.getElementById('streamdeck_rotation');
          if (rotEl) rotEl.value = cfg.rotation.toString();
        }
        if (cfg.brightness !== undefined) {
          const brEl = document.getElementById('streamdeck_brightness');
          if (brEl) brEl.value = cfg.brightness.toString();
        }
        _sdKeys = Array.isArray(cfg.keys) ? cfg.keys.slice() : [];
        _sdPage = 0;
        await _sdRenderGrid();
        if (cfg.weather_location !== undefined) {
          const wlEl = document.getElementById('streamdeck_weather_location');
          if (wlEl) wlEl.value = cfg.weather_location;
        }
        if (cfg.weather_units !== undefined) {
          const wuEl = document.getElementById('streamdeck_weather_units');
          if (wuEl) wuEl.value = cfg.weather_units;
        }
        _sdRenderOverrides(cfg.key_overrides);
        _sdLoadHaSettings(cfg);
      }
    } catch (e) {
      console.warn("Could not load streamdeck config:", e);
    }
    if (!_sdKeys.length) { await _sdRenderGrid(); }

    // Keep polling while either install is in flight.
    if ((r.kiosk && r.kiosk.installing) || (r.streamdeck && r.streamdeck.installing)) {
      clearTimeout(window._hwPoll);
      window._hwPoll = setTimeout(loadHardwareStatus, 4000);
    }
  } catch (e) {
    _renderHwRow('kiosk', null);
    _renderHwRow('streamdeck', null);
  }
}

async function installKiosk(btn) { await _installHw('kiosk', btn, 'kiosk-install-result'); }
async function installStreamDeck(btn) { await _installHw('streamdeck', btn, 'streamdeck-install-result'); }

async function _installHw(name, btn, resultId) {
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Starting...';
  try {
    const r = await fetch('setup/' + name + '/install', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if (d.ok) {
      setResult(resultId, true, 'Installation started. This page will update when the service is running.');
      // Tail the install log until the bridge reports the step is no longer
      // running (FoodAssistant-59z), then refresh hardware status once more.
      let done = false;
      const stopLog = _startLogPolling(name, () => done);
      (async () => {
        for (let i = 0; i < 240 && !done; i++) {
          await _sleep(2000);
          let s;
          try {
            s = await fetch('setup/logs/' + name).then(x => x.json());
          } catch (e) { continue; }
          if (s && s.running === false) done = true;
        }
        done = true;
        stopLog();
        loadHardwareStatus();
        loadHardwareDetect();
      })();
      loadHardwareStatus();
      loadHardwareDetect();
    } else {
      setResult(resultId, false, d.error || 'Failed to start installation.');
    }
  } catch (e) {
    setResult(resultId, false, e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Appliance-only: Network pane

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function wifiStatusHtml(d) {
  const state = d.wifi_state || (d.ssid ? 'connected' : 'disconnected');
  const detail = d.wifi_detail ? escapeHtml(d.wifi_detail) : '';
  // When Wi-Fi is not the active link, show wired status so a Pi on Ethernet
  // does not read as offline (FoodAssistant-7r5).
  const eth = d.ethernet || {};
  const ethBadge = eth.connected
    ? `<span class="badge bg-success me-2"><i class="bi bi-ethernet me-1"></i>Ethernet</span><span class="text-secondary small">Connected${eth.ip ? ' at ' + escapeHtml(eth.ip) : ''}</span>`
    : '';
  // Wi-Fi is joined: always the primary line.
  if (state === 'connected') {
    return `<span class="badge bg-success me-2"><i class="bi bi-wifi me-1"></i>Connected</span><span class="text-secondary small">${escapeHtml(d.ssid)}</span>`;
  }
  // Connected by Ethernet with Wi-Fi simply idle: lead with the wired link and
  // present Wi-Fi as "not in use", never a scary error (FoodAssistant-1idf).
  // The active connection comes from the default route; fall back to a
  // detected Ethernet link when the route is not reported.
  const wired = d.active_connection === 'wired' || (!!eth.connected && d.active_connection !== 'wifi');
  if (wired) {
    const lead = ethBadge || `<span class="badge bg-success me-2"><i class="bi bi-ethernet me-1"></i>Ethernet</span>`;
    if (state === 'no-adapter') {
      return lead + '<div class="text-secondary small mt-1">Connected by Ethernet. This device has no Wi-Fi hardware.</div>';
    }
    return lead
      + '<span class="badge bg-secondary ms-2"><i class="bi bi-wifi-off me-1"></i>Wi-Fi not in use</span>'
      + '<div class="text-secondary small mt-1">Connected by Ethernet. Wi-Fi is available but not in use.</div>';
  }
  switch (state) {
    case 'no-adapter':
      return `<span class="badge bg-secondary me-2"><i class="bi bi-wifi-off me-1"></i>No Wi-Fi adapter</span><span class="text-secondary small">${detail || 'This device has no Wi-Fi hardware. Use Ethernet.'}</span>`;
    case 'no-networkmanager':
      return `<span class="badge bg-warning text-dark me-2"><i class="bi bi-exclamation-triangle me-1"></i>NetworkManager off</span><span class="text-secondary small">${detail || 'NetworkManager is not running.'}</span>`;
    case 'unmanaged':
      return `<span class="badge bg-warning text-dark me-2"><i class="bi bi-exclamation-triangle me-1"></i>Unmanaged</span><span class="text-secondary small">${detail || 'Wi-Fi device is not managed by NetworkManager.'}</span>`;
    default:
      // Disconnected Wi-Fi with no wired link: this is a real "not connected".
      return `<span class="badge bg-secondary me-2"><i class="bi bi-wifi-off me-1"></i>Not connected</span><span class="text-secondary small">No active Wi-Fi</span>`;
  }
}

async function loadNetworkStatus() {
  const wifiEl = document.getElementById('wifi-status-row');
  const hnEl = document.getElementById('hostname-status-row');
  try {
    const r = await fetch('setup/network/status');
    const d = await r.json();
    if (d.ok) {
      if (wifiEl) wifiEl.innerHTML = wifiStatusHtml(d);
      if (hnEl) {
        hnEl.innerHTML = `<span class="text-secondary small">Current hostname: <strong>${d.hostname}</strong> (reachable at <code>${d.hostname}.local</code>)</span>`;
        const inp = document.getElementById('new_hostname');
        if (inp && !inp.value) inp.value = d.hostname;
      }
    } else {
      const msg = `<span class="text-secondary small">Status unavailable: ${d.error}</span>`;
      if (wifiEl) wifiEl.innerHTML = msg;
      if (hnEl) hnEl.innerHTML = msg;
    }
  } catch (e) {
    const msg = '<span class="text-secondary small">Could not reach host bridge. Is the appliance running on a Pi?</span>';
    if (wifiEl) wifiEl.innerHTML = msg;
    if (hnEl) hnEl.innerHTML = msg;
  }
}

async function changeWifi() {
  const ssid = document.getElementById('wifi_ssid')?.value.trim();
  const password = document.getElementById('wifi_password')?.value || '';
  const el = document.getElementById('wifi-result');
  if (!ssid) { if (el) el.innerHTML = '<span class="text-danger">Enter a network name first.</span>'; return; }
  if (el) el.innerHTML = '<span class="text-secondary"><span class="spinner-border spinner-border-sm me-1"></span>Connecting...</span>';
  try {
    const r = await fetch('setup/network/wifi', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, password}),
    });
    const d = await r.json();
    if (el) el.innerHTML = d.ok
      ? `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Connected to <strong>${ssid}</strong>.</span>`
      : `<span class="text-danger">${d.error}</span>`;
    if (d.ok) { loadNetworkStatus(); disableAp(); }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

function pickSsid(ssid) {
  const inp = document.getElementById('wifi_ssid');
  if (inp) { inp.value = ssid; inp.focus(); }
  const pw = document.getElementById('wifi_password');
  if (pw) pw.focus();
}

function signalIcon(signal) {
  if (signal >= 75) return 'bi-reception-4';
  if (signal >= 50) return 'bi-reception-3';
  if (signal >= 25) return 'bi-reception-2';
  return 'bi-reception-1';
}

async function scanWifi() {
  const out = document.getElementById('wifi-scan-results');
  const btn = document.getElementById('wifi-scan-btn');
  if (btn) btn.disabled = true;
  if (out) out.innerHTML = '<span class="text-secondary small"><span class="spinner-border spinner-border-sm me-2"></span>Scanning...</span>';
  try {
    const r = await fetch('setup/network/scan');
    const d = await r.json();
    if (!d.ok) {
      if (out) out.innerHTML = `<span class="text-danger small">${escapeHtml(d.error || 'Scan failed.')}</span>`;
      return;
    }
    const nets = d.networks || [];
    if (!nets.length) {
      const msg = d.detail || 'No networks found.';
      if (out) out.innerHTML = `<span class="text-secondary small">${escapeHtml(msg)}</span>`;
      return;
    }
    const rows = nets.map(n => {
      const secure = n.security && n.security !== '--';
      const lock = secure ? '<i class="bi bi-lock-fill text-secondary ms-2" title="Secured"></i>' : '';
      return `<button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center py-1" onclick="pickSsid('${escapeHtml(n.ssid).replace(/'/g, "\\'")}')">
        <span><i class="bi ${signalIcon(n.signal)} me-2"></i>${escapeHtml(n.ssid)}${lock}</span>
        <span class="text-secondary small">${n.signal}%</span>
      </button>`;
    }).join('');
    if (out) out.innerHTML = `<div class="list-group">${rows}</div>`;
  } catch (e) {
    if (out) out.innerHTML = `<span class="text-danger small">${escapeHtml(e)}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function changeHostname() {
  const hostname = document.getElementById('new_hostname')?.value.trim().toLowerCase() || '';
  const el = document.getElementById('hostname-result');
  if (!hostname) { if (el) el.innerHTML = '<span class="text-danger">Enter a hostname.</span>'; return; }
  if (el) el.innerHTML = '<span class="text-secondary">Updating...</span>';
  try {
    const r = await fetch('setup/network/hostname', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hostname}),
    });
    const d = await r.json();
    if (d.ok) {
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Hostname changed to <strong>${hostname}</strong>. Reconnect at <a href="http://${hostname}.local:9284/setup">http://${hostname}.local:9284/setup</a>.</span>`;
      loadNetworkStatus();
    } else {
      if (el) el.innerHTML = `<span class="text-danger">${d.error}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

// -- Satellite devices (main server) ----------------------------------------

function deviceRowHtml(d) {
  const name = d.label || d.hostname || d.device_id || 'unknown device';
  const online = d.online
    ? '<span class="badge bg-success">Online</span>'
    : '<span class="badge bg-secondary">Offline</span>';
  // A pi_remote satellite serves the app on port 80; a server or pi_hosted box
  // serves on 9284. Build the link to match so it does not 404 on the wrong port.
  const devPort = d.deployment_mode === 'pi_remote' ? '' : ':9284';
  const ip = d.ip
    ? `<a href="http://${escapeHtml(d.ip)}${devPort}/" target="_blank" rel="noopener">${escapeHtml(d.ip)}</a>`
    : '<span class="text-secondary">--</span>';
  const mode = d.deployment_mode ? escapeHtml(d.deployment_mode) : '--';
  // Traffic-light version status vs this server (FoodAssistant-469o):
  // green = same, yellow = patch differs, red = major/minor differs.
  const vbadge = {
    same:        ' <span class="badge bg-success" title="Same version as the server">current</span>',
    patch:       ' <span class="badge bg-warning text-dark" title="Different patch version">patch differs</span>',
    major_minor: ' <span class="badge bg-danger" title="Different major/minor version">update needed</span>',
  }[d.version_diff] || '';
  const version = (d.version ? escapeHtml(d.version) : '--') + vbadge;
  const seen = d.last_seen ? escapeHtml(d.last_seen) : '--';
  const id = escapeHtml(d.device_id || '').replace(/'/g, "\\'");
  return `<tr>
    <td>${online}<div class="small fw-semibold mt-1">${escapeHtml(name)}</div></td>
    <td class="small">${ip}</td>
    <td class="small">${mode}</td>
    <td class="small">${version}</td>
    <td class="small text-secondary">${seen}</td>
    <td class="text-end text-nowrap">
      <button class="btn btn-outline-secondary btn-sm" onclick="resyncDevice('${id}')" title="Queue a resync"><i class="bi bi-arrow-repeat"></i></button>
      <button class="btn btn-outline-danger btn-sm" onclick="forgetDevice('${id}')" title="Forget this device"><i class="bi bi-trash"></i></button>
    </td>
  </tr>`;
}

async function loadDevices() {
  const out = document.getElementById('devices-list');
  if (!out) return;
  out.innerHTML = '<span class="text-secondary small"><span class="spinner-border spinner-border-sm me-2"></span>Loading...</span>';
  try {
    const r = await fetch('api/devices');
    const d = await r.json();
    const devs = d.devices || [];
    if (!devs.length) {
      out.innerHTML = '<span class="text-secondary small">No devices yet. A satellite appears here after its first config sync, or run a LAN scan below.</span>';
      return;
    }
    const rows = devs.map(deviceRowHtml).join('');
    out.innerHTML = `<div class="table-responsive"><table class="table table-sm align-middle mb-0">
      <thead><tr class="small text-secondary">
        <th>Device</th><th>IP</th><th>Mode</th><th>Version</th><th>Last seen</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  } catch (e) {
    out.innerHTML = `<span class="text-danger small">${escapeHtml(String(e))}</span>`;
  } finally {
    loadPairingRequests();
    startPairingPoll();
  }
}

// Poll pending pairing requests while the Devices pane is on screen, so a
// request that arrives after the pane is open shows up on its own instead of
// only after a manual refresh (FoodAssistant-4box follow-up, Dan 2026-07-11).
// Self-stopping: the tick checks the pane is still visible and clears itself
// otherwise, so it never runs in the background on another pane.
let _pairingPollTimer = null;
function startPairingPoll() {
  if (_pairingPollTimer) return;
  _pairingPollTimer = setInterval(() => {
    const out = document.getElementById('pairing-requests');
    // offsetParent is null when the element (or an ancestor pane) is hidden.
    if (!out || out.offsetParent === null) {
      clearInterval(_pairingPollTimer);
      _pairingPollTimer = null;
      return;
    }
    loadPairingRequests();
  }, 5000);
}

// LAN device pairing (FoodAssistant-4box): a new satellite asks to join and the
// user approves here after matching the code shown on the device's own screen.
async function loadPairingRequests() {
  const out = document.getElementById('pairing-requests');
  if (!out) return;
  try {
    const r = await fetch('api/pairing/pending');
    const d = await r.json();
    const reqs = d.requests || [];
    if (!reqs.length) {
      out.innerHTML = '<span class="text-secondary small">No devices are asking to join right now.</span>';
      return;
    }
    out.innerHTML = reqs.map((q) => {
      const id = escapeHtml(q.request_id || '').replace(/'/g, "\\'");
      const who = escapeHtml(q.hostname || 'Unnamed device');
      const ip = q.ip ? ` <span class="text-secondary">(${escapeHtml(q.ip)})</span>` : '';
      return `<div class="d-flex align-items-center flex-wrap gap-2 border rounded p-2 mb-2">
        <div>
          <div class="small fw-semibold">${who}${ip}</div>
          <div class="small text-secondary">Code on the device: <span class="fs-5 fw-bold font-monospace">${escapeHtml(q.code || '')}</span></div>
        </div>
        <div class="ms-auto text-nowrap">
          <button class="btn btn-success btn-sm" onclick="approvePairing('${id}')"><i class="bi bi-check-lg me-1"></i>Approve</button>
          <button class="btn btn-outline-danger btn-sm" onclick="denyPairing('${id}')"><i class="bi bi-x-lg me-1"></i>Deny</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    out.innerHTML = `<span class="text-danger small">${escapeHtml(String(e))}</span>`;
  }
}

async function approvePairing(id) {
  try {
    await fetch('api/pairing/approve', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({request_id: id}),
    });
  } catch (e) { /* the refresh below shows the outcome */ }
  loadPairingRequests();
}

async function denyPairing(id) {
  try {
    await fetch('api/pairing/deny', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({request_id: id}),
    });
  } catch (e) { /* the refresh below shows the outcome */ }
  loadPairingRequests();
}

async function resyncDevice(id) {
  try {
    await fetch(`api/devices/${encodeURIComponent(id)}/command`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: 'resync'}),
    });
  } catch (e) { /* best effort */ }
  loadDevices();
}

async function forgetDevice(id) {
  if (!confirm('Forget this device? It reappears on its next check-in.')) return;
  try {
    await fetch(`api/devices/${encodeURIComponent(id)}`, {method: 'DELETE'});
  } catch (e) { /* best effort */ }
  loadDevices();
}

async function scanLan() {
  const btn = document.getElementById('scan-lan-btn');
  const out = document.getElementById('scan-result');
  if (btn) btn.disabled = true;
  if (out) { out.className = 'test-result text-secondary'; out.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Scanning the network, this can take a moment...'; }
  try {
    const r = await fetch('api/devices/scan', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cidr: val('scan_cidr') || ''}),
    });
    const d = await r.json();
    if (!d.ok) {
      if (out) { out.className = 'test-result text-danger'; out.textContent = d.error || 'Scan failed.'; }
      return;
    }
    const found = d.found || [];
    const where = d.cidr ? ` on ${escapeHtml(d.cidr)}` : '';
    if (out) {
      // A Docker-bridge default means we scanned the container network, not the
      // LAN, so tell the user to enter their real range (their satellites live
      // there, not on 172.x).
      const dockerHint = (!found.length && d.dockerish)
        ? ' That looks like a Docker network, not your LAN. Enter your LAN range above (for example 192.168.1.0/24) and scan again.'
        : '';
      out.className = 'test-result ' + (found.length ? 'text-success' : (d.dockerish ? 'text-warning' : 'text-secondary'));
      out.textContent = (found.length
        ? `Found ${found.length} instance(s)${where}.`
        : `No instances found${where}.`) + dockerHint;
    }
    loadDevices();
  } catch (e) {
    if (out) { out.className = 'test-result text-danger'; out.textContent = String(e); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function checkApStatus() {
  try {
    const r = await fetch('setup/ap/status');
    const d = await r.json();
    const banner = document.getElementById('ap-fallback-banner');
    if (banner && d.active) {
      banner.classList.remove('d-none');
    }
  } catch (e) {
    // Bridge not reachable (non-Pi or bridge not running); silently ignore.
  }
}

async function disableAp() {
  try {
    await fetch('setup/ap/disable', {method: 'POST'});
    const banner = document.getElementById('ap-fallback-banner');
    if (banner) banner.classList.add('d-none');
  } catch (e) {
    // Best effort; if it fails the AP will time out on its own.
  }
}

// Read-only attached-hardware panel: fetch live display + Stream Deck presence
// from the host bridge (via the app proxy) and fill the badges. Safe off a Pi:
// the proxy returns a clean "nothing attached" shape there.
async function loadHardwareDetect() {
  const dEl = document.getElementById('hwdetect-display');
  const sEl = document.getElementById('hwdetect-streamdeck');
  if (!dEl && !sEl) return;
  let r;
  try {
    r = await fetch('setup/hardware/status').then(x => x.json());
  } catch (e) {
    r = null;
  }
  const display = (r && r.display) || {present: false, connectors: []};
  const sd = (r && r.streamdeck) || {present: false, model: ''};
  if (dEl) {
    if (display.present) {
      const names = (display.connectors || []).join(', ');
      dEl.innerHTML = '<span class="badge bg-success">Detected' +
        (names ? ' (' + names + ')' : '') + '</span>';
    } else {
      dEl.innerHTML = '<span class="badge bg-secondary">Not detected</span>';
    }
  }
  if (sEl) {
    if (sd.present) {
      const model = sd.model || '';
      sEl.innerHTML = '<span class="badge bg-success">Detected' +
        (model ? ' (' + model + ')' : '') + '</span>';
    } else {
      sEl.innerHTML = '<span class="badge bg-secondary">Not detected</span>';
    }
  }
  _renderEnableHint('kiosk', display.present, r && r.kiosk);
  _renderEnableHint('streamdeck', sd.present, r && r.streamdeck);
  _applyDeckAutodetect(sd);
  _applyDisplayAutodetect(display);
}

// Prefill the wizard's display switch from live detection: a connected
// display flips "HDMI display connected to this device" on and reveals the
// display options, mirroring the Stream Deck auto-detect. Never flips the
// switch off, so a manual choice sticks.
function _applyDisplayAutodetect(display) {
  if (!display || !display.present) return;
  const sw = document.getElementById('wiz_has_display');
  if (sw && !sw.checked) {
    sw.checked = true;
    sw.dispatchEvent(new Event('change'));
  }
}

// "Detected, but not set up" hints with a one-click Enable button. The same
// hint renders in the settings Hardware pane and in the wizard's Hardware
// step (whichever ids exist on the page). Only shown when the appliance
// reports the hardware attached and the matching service never provisioned,
// so an already-installed service is never offered a reinstall.
function _renderEnableHint(name, present, st) {
  const show = !!(st && present && !st.installed && !st.active && !st.installing);
  ['hwdetect-' + name + '-hint', 'wiz-hwdetect-' + name + '-hint'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('d-none', !show);
  });
}

// Prefill the deck size from the bridge's live detection (FoodAssistant-dcrh).
// The bridge reports key_count for known Elgato product IDs; when present, set
// the model dropdown, reveal the Stream Deck options, flip the "connected"
// switch on, and show the auto-detected hint. Runs in both the wizard and the
// settings pane (each has at most one of these elements in the DOM).
function _applyDeckAutodetect(sd) {
  if (!sd || !sd.present || !sd.key_count) return;
  const kcEl = document.getElementById('streamdeck_key_count');
  if (kcEl) {
    kcEl.value = String(sd.key_count);
    kcEl.dispatchEvent(new Event('change'));
  }
  const sw = document.getElementById('has_streamdeck');
  if (sw && !sw.checked) {
    sw.checked = true;
    sw.dispatchEvent(new Event('change'));
  }
  document.querySelectorAll('.sd-autodetect-hint').forEach(function (el) {
    el.classList.remove('d-none');
  });
}

// Pi system warnings banner (FoodAssistant-y06w). Fills the dismissable alert
// on the Devices pane from the host bridge's continuous monitor (undervoltage,
// throttling, heat, storage). One fetch per settings visit is enough: these
// conditions change slowly, and the navbar indicator plus the action-items
// inbox carry the ongoing state. The banner only exists on a Pi (the template
// gates it), so this exits immediately everywhere else.
async function loadSystemWarnings() {
  const banner = document.getElementById('system-warnings-banner');
  const list = document.getElementById('system-warnings-list');
  if (!banner || !list) return;
  try {
    const d = await fetch('setup/system/health', { cache: 'no-store' }).then(x => x.json());
    const warnings = (d && d.warnings) || [];
    if (!warnings.length) { banner.classList.add('d-none'); return; }
    list.innerHTML = '';
    let showPowerHelp = false;
    warnings.forEach(function (w) {
      const li = document.createElement('li');
      li.textContent = w.message || w.key || 'Device warning';
      list.appendChild(li);
      if (w.key === 'undervoltage' || w.key === 'freq_capped' || w.key === 'throttled') {
        showPowerHelp = true;
      }
    });
    const help = document.getElementById('system-warnings-help');
    if (help) {
      help.innerHTML = '';
      if (showPowerHelp) {
        help.append('Power trouble is almost always the supply or a charge-only USB cable. ');
        const a = document.createElement('a');
        a.href = 'https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/docs/hardware.md#power-and-cabling';
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = 'Power and cabling guide';
        help.appendChild(a);
      }
    }
    banner.classList.remove('d-none');
  } catch (e) { /* transient: leave the banner as it is */ }
}

document.addEventListener('DOMContentLoaded', loadSystemWarnings);

// -- Hardware presets (FoodAssistant-kl5n) ----------------------------------
// Apply a prebuilt bundle of hardware settings in one click, from the wizard's
// Hardware step or the Screen & Sleep pane. The server saves only the preset's
// fields and pushes the deck config; here the applied values are reflected back
// into the form so the user sees what changed and can still fine-tune.

async function applyHardwarePreset(selectId) {
  const sel = document.getElementById(selectId);
  const out = document.querySelector('.hw-preset-result');
  const preset = sel && sel.value;
  const setOut = (cls, html) => { if (out) { out.className = 'test-result ' + cls; out.innerHTML = html; } };
  if (!preset) { setOut('text-warning', 'Pick a preset first.'); return; }
  setOut('text-secondary', '<span class="spinner-border spinner-border-sm me-1"></span>Applying...');
  let d;
  try {
    const r = await fetch('setup/preset/apply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({preset}),
    });
    d = await r.json();
  } catch (e) {
    setOut('text-danger', String(e));
    return;
  }
  if (!d || !d.ok) {
    setOut('text-danger', (d && d.error) || 'Could not apply the preset.');
    return;
  }
  const label = d.label || 'preset';
  if (selectId.endsWith('_wiz')) {
    // In the wizard the display fields use _wiz-suffixed ids whose change
    // handlers mirror the value into the hidden settings inputs; reflect the
    // applied bundle in place so the wizard step stays put.
    _applyPresetToWizard(d.applied || {});
    setOut('text-success', '<i class="bi bi-check-circle me-1"></i>Applied ' + label + '. Adjust anything below if needed.');
  } else {
    // In Settings the bundle spans two panes (Screen and Stream Deck), so a
    // reload is the simplest way to show every saved value.
    setOut('text-success', '<i class="bi bi-check-circle me-1"></i>Applied ' + label + '. Reloading...');
    setTimeout(function () { window.location.reload(); }, 700);
  }
}

function _presetSetField(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.type === 'checkbox') { el.checked = !!value; }
  else { el.value = String(value); }
  el.dispatchEvent(new Event('change'));
}

function _applyPresetToWizard(applied) {
  const displayTouched = ['ui_scale', 'display_rotation', 'display_type', 'display_touch']
    .some(function (k) { return k in applied; });
  if (displayTouched) {
    const disp = document.getElementById('wiz_has_display');
    if (disp && !disp.checked) { disp.checked = true; disp.dispatchEvent(new Event('change')); }
  }
  if ('ui_scale' in applied) _presetSetField('ui_scale_wiz', applied.ui_scale);
  if ('display_rotation' in applied) _presetSetField('display_rotation_wiz', applied.display_rotation);
  if ('display_type' in applied) _presetSetField('display_type_wiz', applied.display_type);
  if ('display_touch' in applied) _presetSetField('display_touch', applied.display_touch);
  if ('has_streamdeck' in applied) _presetSetField('has_streamdeck', applied.has_streamdeck);
  if ('streamdeck_key_count' in applied) _presetSetField('streamdeck_key_count', applied.streamdeck_key_count);
}
