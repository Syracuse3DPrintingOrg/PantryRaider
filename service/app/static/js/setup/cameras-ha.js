// Save the camera feeds from the Interface pane. Persists to app settings (which
// power the on-screen Camera page on any device) and, when a deck/bridge is
// reachable, pushes the snapshot URLs into the deck config too. The deck push is
// best-effort: a non-Pi browser just saves the app settings.
async function savePaneCameras(btn) {
  const el = document.getElementById('cameras-result');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving...'; }
  if (el) el.innerHTML = '';
  const cameras = _sdCollectCameras();
  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({streamdeck_cameras: cameras}),
    });
    try {
      let base = {};
      const cur = await fetch('setup/streamdeck/config').then(x => x.json());
      if (cur && cur.ok && cur.config && typeof cur.config === 'object') base = cur.config;
      const merged = Object.assign({}, base, {
        cameras: cameras.map(c => ({name: c.name, snapshot_url: c.snapshot_url})),
      });
      const r = await fetch('setup/streamdeck/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({config: merged}),
      });
      if (r.ok) await fetch('setup/streamdeck/restart', {method: 'POST'});
    } catch (e) { /* no bridge reachable: app settings are still saved */ }
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Cameras saved.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Manual camera setup by IP (FoodAssistant-zo7y). Each preset describes how to
// build a stream and a snapshot URL for a common camera brand. credMode says how
// the login is carried: "url" embeds user:pass@ in the URL (HTTP basic auth),
// "query" appends &user=&password= (Reolink-style), "none" omits it. A blank
// stream means the camera only offers RTSP (not playable in a browser or on the
// deck), so we leave the live feed empty and rely on the snapshot. showPath
// reveals the editable stream path field. Generated URLs are pre-filled into a
// new camera row, where the user can adjust them before saving.
const _CAM_IP_PRESETS = {
  generic_mjpeg:    {label: 'Generic MJPEG',    port: '', streamPath: '/video', snapshotPath: '/snapshot', credMode: 'url', showPath: true},
  generic_snapshot: {label: 'Generic snapshot', port: '', streamPath: '',       snapshotPath: '/snapshot.jpg', credMode: 'url', showPath: true, snapshotOnly: true},
  reolink:          {label: 'Reolink',          port: '', streamPath: '',        snapshotPath: '/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=foodassistant', credMode: 'query'},
  amcrest:          {label: 'Amcrest/Dahua',    port: '', streamPath: '/cgi-bin/mjpg/video.cgi?channel=1&subtype=1', snapshotPath: '/cgi-bin/snapshot.cgi?channel=1', credMode: 'url'},
  hikvision:        {label: 'Hikvision',        port: '', streamPath: '/ISAPI/Streaming/channels/102/httpPreview',  snapshotPath: '/ISAPI/Streaming/channels/101/picture', credMode: 'url'},
  onvif:            {label: 'ONVIF',            port: '', streamPath: '',        snapshotPath: '/onvif/snapshot', credMode: 'url', snapshotOnly: true},
  custom:           {label: 'Custom',           port: '', streamPath: '/',       snapshotPath: '', credMode: 'url', showPath: true},
};

// Build the host[:port] portion with optional embedded credentials.
function _camAuthority(host, port, user, pass, credMode) {
  let auth = '';
  if (credMode === 'url' && user) {
    auth = encodeURIComponent(user) + (pass ? ':' + encodeURIComponent(pass) : '') + '@';
  }
  return auth + host + (port ? ':' + port : '');
}

// Append &user=&password= for brands that pass the login in the query string.
function _camWithQueryCreds(url, user, pass, credMode) {
  if (credMode !== 'query' || !user) return url;
  const sep = url.includes('?') ? '&' : '?';
  return url + sep + 'user=' + encodeURIComponent(user) +
         '&password=' + encodeURIComponent(pass || '');
}

function _camBuildUrls(presetKey, host, port, user, pass, pathOverride) {
  const p = _CAM_IP_PRESETS[presetKey] || _CAM_IP_PRESETS.generic_mjpeg;
  const authority = _camAuthority(host, port || p.port, user, pass, p.credMode);
  const streamPath = (pathOverride !== undefined && pathOverride !== '') ? pathOverride : p.streamPath;
  let stream = '';
  if (streamPath) stream = _camWithQueryCreds('http://' + authority + streamPath, user, pass, p.credMode);
  let snapshot = '';
  if (p.snapshotPath) snapshot = _camWithQueryCreds('http://' + authority + p.snapshotPath, user, pass, p.credMode);
  // A snapshot-only camera still needs something live for the kiosk page; reuse
  // the snapshot there so the page shows a still rather than nothing.
  if (!stream && snapshot) stream = snapshot;
  return {stream_url: stream, snapshot_url: snapshot};
}

// React to the brand dropdown: show the editable stream-path field only for the
// presets that take one, and pre-fill it with the preset default.
function _camPresetChanged() {
  const key = document.getElementById('cam-ip-preset')?.value || 'generic_mjpeg';
  const p = _CAM_IP_PRESETS[key] || {};
  const wrap = document.querySelector('.sd-cam-ip-pathwrap');
  if (wrap) wrap.classList.toggle('d-none', !p.showPath);
  const pathEl = document.getElementById('cam-ip-path');
  if (pathEl && p.showPath) pathEl.value = p.streamPath || '';
}

// Pre-fill the scan box with the server's best LAN guess so it is visible and
// correctable before scanning (FoodAssistant-d9rx).
// True for Docker's private bridge range (172.16-31.x), which is not the LAN.
function _camIsDockerish(cidr) {
  return /^\s*172\.(1[6-9]|2[0-9]|3[01])\./.test(cidr || '');
}

// Show or hide the "this is a Docker subnet, not your LAN" note based on the
// current box value, so it updates live as the user types (FoodAssistant-d9rx).
function _camUpdateScanHint() {
  const el = document.getElementById('cam-scan-cidr');
  const hint = document.getElementById('cam-scan-hint');
  if (!el || !hint) return;
  if (_camIsDockerish(el.value)) {
    hint.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + el.value.trim() +
      ' is this app’s Docker network, not your LAN. Change it to your home network ' +
      '(e.g. 192.168.1.0/24) before scanning.';
    hint.classList.remove('d-none');
  } else {
    hint.classList.add('d-none');
  }
}

async function _camPrefillScanCidr() {
  const el = document.getElementById('cam-scan-cidr');
  if (!el) return;
  if (!el.value.trim()) {
    try {
      const d = await fetch('setup/cameras/scan-default').then(r => r.json());
      if (d && d.cidr) el.value = d.cidr;
    } catch (e) { /* leave it blank */ }
  }
  _camUpdateScanHint();
}

// Scan the LAN for IP cameras and list them with Preview + Add, like the HA
// camera discovery (FoodAssistant-d9rx).
// Build one scan-result row: the IP plus any detected brand/resolution, a
// Preview and Add when a snapshot was found, and an inline login form (user +
// password) for a password-protected or snapshot-less camera that re-probes
// with credentials to find a working snapshot (FoodAssistant-ij6w).
function _camScanRow(cam) {
  const item = document.createElement('div');
  item.className = 'list-group-item py-2';
  const top = document.createElement('div');
  top.className = 'd-flex justify-content-between align-items-center flex-wrap gap-2';

  const label = document.createElement('span');
  label.className = 'small';
  const bits = [];
  if (cam.brand) bits.push(cam.brand);
  if (cam.resolution) bits.push(cam.resolution);
  const detail = cam.snapshot_url ? 'snapshot found' :
    cam.auth_required ? 'needs login (password-protected)' :
    cam.rtsp ? 'RTSP only (needs a snapshot/MJPEG path or a bridge)' :
    'open ports ' + cam.ports.join(', ');
  label.innerHTML = '<strong>' + cam.ip + '</strong>' +
    (bits.length ? ' <span class="badge bg-secondary">' + bits.join(' · ') + '</span>' : '') +
    ' <span class="text-secondary">(' + detail + ')</span>';

  const btns = document.createElement('div');
  btns.className = 'd-flex gap-1';
  // State that the Add/Preview buttons read; a successful login probe updates it.
  const state = { snapshot_url: cam.snapshot_url || '', name: cam.name };

  const prev = document.createElement('button');
  prev.type = 'button'; prev.className = 'btn btn-sm btn-outline-info';
  prev.innerHTML = '<i class="bi bi-eye"></i> Preview';
  prev.onclick = () => _camPreview({ name: state.name, snapshot_url: state.snapshot_url });
  if (!state.snapshot_url) prev.classList.add('d-none');
  btns.appendChild(prev);

  const add = document.createElement('button');
  add.type = 'button'; add.className = 'btn btn-sm btn-outline-success';
  add.innerHTML = '<i class="bi bi-plus-lg"></i> Add';
  add.onclick = () => {
    _sdAddCameraRow({ name: state.name, snapshot_url: state.snapshot_url || '', stream_url: '' });
    add.disabled = true; add.className = 'btn btn-sm btn-outline-secondary';
    add.innerHTML = '<i class="bi bi-check2"></i> Added';
  };
  btns.appendChild(add);

  // Offer a login form for cameras we could not snapshot anonymously.
  let loginBtn = null;
  if (!cam.snapshot_url) {
    loginBtn = document.createElement('button');
    loginBtn.type = 'button'; loginBtn.className = 'btn btn-sm btn-outline-secondary';
    loginBtn.innerHTML = '<i class="bi bi-key"></i> Add login';
    btns.appendChild(loginBtn);
  }
  top.appendChild(label); top.appendChild(btns);
  item.appendChild(top);

  if (!cam.snapshot_url) {
    const form = document.createElement('div');
    form.className = 'row g-1 align-items-center mt-2 d-none';
    form.innerHTML =
      '<div class="col-auto"><input type="text" class="form-control form-control-sm cam-login-user" placeholder="username" autocomplete="off" style="max-width:140px"></div>' +
      '<div class="col-auto"><input type="password" class="form-control form-control-sm cam-login-pass" placeholder="password" autocomplete="new-password" style="max-width:140px"></div>' +
      '<div class="col-auto"><button type="button" class="btn btn-sm btn-outline-info cam-login-test"><i class="bi bi-box-arrow-in-right"></i> Test &amp; preview</button></div>' +
      '<div class="col-12"><div class="small mt-1 cam-login-msg"></div></div>';
    item.appendChild(form);
    loginBtn.onclick = () => form.classList.toggle('d-none');
    form.querySelector('.cam-login-test').onclick = async (ev) => {
      const b = ev.currentTarget;
      const user = form.querySelector('.cam-login-user').value.trim();
      const pass = form.querySelector('.cam-login-pass').value;
      const msg = form.querySelector('.cam-login-msg');
      const o = b.innerHTML; b.disabled = true;
      b.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
      msg.innerHTML = '';
      try {
        const d = await fetch('setup/cameras/probe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ip: cam.ip, username: user, password: pass }),
        }).then(r => r.json());
        if (!d.ok) { msg.innerHTML = '<span class="text-danger">' + (d.error || 'No snapshot found.') + '</span>'; return; }
        state.snapshot_url = d.snapshot_url;
        const extra = [d.brand, d.resolution].filter(Boolean).join(' · ');
        msg.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Snapshot found' + (extra ? ' (' + extra + ')' : '') + '. Preview or Add it.</span>';
        prev.classList.remove('d-none');
        _camPreview({ name: state.name, snapshot_url: state.snapshot_url });
      } catch (e) {
        msg.innerHTML = '<span class="text-danger">' + e + '</span>';
      } finally { b.disabled = false; b.innerHTML = o; }
    };
  }
  return item;
}

async function _camScanLan(btn) {
  const out = document.getElementById('cam-scan-results');
  const cidr = document.getElementById('cam-scan-cidr')?.value.trim() || '';
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Scanning...';
  out.innerHTML = '<span class="text-secondary small">Scanning the network, this can take up to a minute...</span>';
  try {
    const data = await fetch('setup/cameras/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cidr }),
    }).then(r => r.json());
    if (!data.ok) { out.innerHTML = `<span class="text-danger small">${data.error || 'Scan failed.'}</span>`; return; }
    // Reflect the network that was scanned back into the box so the user can
    // see and correct it (e.g. when we guessed a Docker subnet).
    if (data.cidr) document.getElementById('cam-scan-cidr').value = data.cidr;
    const hint = data.hint
      ? `<div class="alert alert-warning py-1 px-2 small mb-2">${data.hint}</div>` : '';
    const cams = data.cameras || [];
    if (!cams.length) {
      const note = data.note
        ? `<div class="alert alert-info py-1 px-2 small mb-0">${data.note}</div>`
        : '<span class="text-secondary small">No cameras found on ' + data.cidr + '.</span>';
      out.innerHTML = hint + note;
      return;
    }
    out.innerHTML = hint;
    const list = document.createElement('div');
    list.className = 'list-group';
    cams.forEach(cam => { list.appendChild(_camScanRow(cam)); });
    out.innerHTML = hint;   // keep the Docker-subnet warning above the results
    out.appendChild(list);
  } catch (e) {
    out.innerHTML = `<span class="text-danger small">${e}</span>`;
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

function _camAddFromIp() {
  const el = document.getElementById('cam-ip-result');
  const key = document.getElementById('cam-ip-preset')?.value || 'generic_mjpeg';
  const name = document.getElementById('cam-ip-name')?.value.trim() || '';
  const host = document.getElementById('cam-ip-host')?.value.trim() || '';
  const port = document.getElementById('cam-ip-port')?.value.trim() || '';
  const user = document.getElementById('cam-ip-user')?.value.trim() || '';
  const pass = document.getElementById('cam-ip-pass')?.value || '';
  const path = document.getElementById('cam-ip-path')?.value.trim();
  if (!host) {
    if (el) el.innerHTML = '<span class="text-danger">Enter the camera IP or host.</span>';
    return;
  }
  const urls = _camBuildUrls(key, host, port, user, pass, path);
  _sdAddCameraRow({name: name || host, stream_url: urls.stream_url, snapshot_url: urls.snapshot_url});
  const note = _CAM_IP_PRESETS[key] && _CAM_IP_PRESETS[key].snapshotOnly
    ? ' This brand commonly offers only RTSP for live video, so the live feed reuses the snapshot. Edit the row if your camera has an MJPEG or HLS URL.'
    : '';
  if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Added a row below. Review the URLs, then Save cameras.' + note + '</span>';
}

// Discover cameras from a Frigate recorder and list them with Preview + Add,
// mirroring the Home Assistant discovery (FoodAssistant-7ror). Frigate carries no
// login, so its still URL is safe to preview directly and to store as a plain
// camera the existing resolve/proxy path serves.
async function _camFrigateDiscover(btn) {
  const out = document.getElementById('cam-frigate-results');
  const res = document.getElementById('cam-frigate-result');
  const url = document.getElementById('cam-frigate-url')?.value.trim() || '';
  const orig = btn ? btn.innerHTML : '';
  if (res) res.innerHTML = '';
  if (out) out.innerHTML = '';
  if (!url) {
    if (res) res.innerHTML = '<span class="text-danger">Enter your Frigate address.</span>';
    return;
  }
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Searching...'; }
  try {
    const data = await fetch('setup/cameras/frigate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base_url: url}),
    }).then(r => r.json());
    if (!data || !data.ok) {
      if (res) res.innerHTML = `<span class="text-danger">${(data && data.error) || 'Could not reach Frigate.'}</span>`;
      return;
    }
    const cams = data.cameras || [];
    if (!cams.length) {
      if (out) out.innerHTML = '<span class="text-secondary small">No cameras found on Frigate.</span>';
      return;
    }
    // Skip cameras whose snapshot URL is already in the list.
    const have = new Set(_sdCollectCameras().map(c => c.snapshot_url).filter(Boolean));
    const list = document.createElement('div');
    list.className = 'list-group';
    cams.forEach(cam => {
      const already = have.has(cam.snapshot_url);
      const item = document.createElement('div');
      item.className = 'list-group-item d-flex justify-content-between align-items-center py-1';
      const label = document.createElement('span');
      label.className = 'small';
      label.textContent = cam.name;
      const btns = document.createElement('div');
      btns.className = 'd-flex gap-1';
      const prev = document.createElement('button');
      prev.type = 'button'; prev.className = 'btn btn-sm btn-outline-info';
      prev.innerHTML = '<i class="bi bi-eye"></i> Preview';
      prev.onclick = () => _camPreview({name: cam.name, snapshot_url: cam.snapshot_url});
      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'btn btn-sm ' + (already ? 'btn-outline-secondary' : 'btn-outline-success');
      add.innerHTML = already ? '<i class="bi bi-check2"></i> Added' : '<i class="bi bi-plus-lg"></i> Add';
      add.disabled = already;
      add.onclick = () => {
        _sdAddCameraRow({name: cam.name, stream_url: cam.stream_url || '', snapshot_url: cam.snapshot_url || ''});
        add.disabled = true; add.className = 'btn btn-sm btn-outline-secondary';
        add.innerHTML = '<i class="bi bi-check2"></i> Added';
      };
      btns.appendChild(prev); btns.appendChild(add);
      item.appendChild(label); item.appendChild(btns);
      list.appendChild(item);
    });
    if (out) out.appendChild(list);
    if (res) res.innerHTML = '<span class="text-secondary small">Preview a camera, then Add it. Review the list below and Save cameras.</span>';
  } catch (e) {
    if (res) res.innerHTML = `<span class="text-danger">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Read the Reolink add form into a request body. Shared by the Preview and Add
// buttons so both send the same host/channel/login the server signs in with.
function _camReolinkBody() {
  const host = document.getElementById('cam-reo-host')?.value.trim() || '';
  return {
    name: document.getElementById('cam-reo-name')?.value.trim() || '',
    host,
    port: document.getElementById('cam-reo-port')?.value.trim() || '',
    channel: parseInt(document.getElementById('cam-reo-channel')?.value || '0', 10) || 0,
    username: document.getElementById('cam-reo-user')?.value.trim() || '',
    password: document.getElementById('cam-reo-pass')?.value || '',
    stream_quality: document.getElementById('cam-reo-quality')?.value || 'main',
  };
}

// Preview a Reolink camera before adding it (FoodAssistant-26mf). The login is
// posted in the request body, never in a URL: the server signs in with the
// token flow and streams the picture back, which the shared preview modal shows
// through a blob. Nothing is saved. A wrong login or an unreachable camera
// shows the same friendly message the Add button gives.
function _camPreviewReolink() {
  const el = document.getElementById('cam-reo-result');
  const body = _camReolinkBody();
  if (!body.host) {
    if (el) el.innerHTML = '<span class="text-danger">Enter the camera address.</span>';
    return;
  }
  if (el) el.innerHTML = '';
  _camPreview({name: body.name || body.host, postUrl: 'setup/cameras/reolink/preview', postBody: body});
}

// Add a Reolink camera by host + login (FoodAssistant-qft4). The server composes
// and stores the credentialed URLs and verifies the picture; the login never
// reaches the page. On success a compact row is added to the list below (its
// hidden fields carry the host/channel/user so a later Save keeps the entry).
async function _camAddReolink(btn) {
  const el = document.getElementById('cam-reo-result');
  const orig = btn ? btn.innerHTML : '';
  if (el) el.innerHTML = '';
  const host = document.getElementById('cam-reo-host')?.value.trim() || '';
  if (!host) {
    if (el) el.innerHTML = '<span class="text-danger">Enter the camera address.</span>';
    return;
  }
  const body = _camReolinkBody();
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Adding...'; }
  try {
    const data = await fetch('setup/cameras/reolink', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (!data || !data.ok) {
      if (el) el.innerHTML = `<span class="text-danger">${(data && data.error) || 'Could not add the camera.'}</span>`;
      return;
    }
    _sdAddReolinkRow(data.camera || {name: body.name || host, host, port: body.port, channel: body.channel, username: body.username, stream_quality: body.stream_quality});
    // Clear the login so the typed secret is not left on screen.
    const p = document.getElementById('cam-reo-pass'); if (p) p.value = '';
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Camera added and saved. It shows in the list below and on the Camera page.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Append a compact Reolink row to the camera list. The password is never placed
// in the DOM; the host/channel/user ride hidden fields so a Save cameras keeps
// the entry and the server restores the stored password.
function _sdAddReolinkRow(cam) {
  const wrap = document.getElementById('streamdeck-cameras');
  if (!wrap) return;
  cam = cam || {};
  const div = document.createElement('div');
  div.className = 'row g-2 align-items-end sd-camera-row';
  div.dataset.source = 'reolink';
  const host = cam.host || '';
  const port = cam.port || '';
  const channel = (cam.channel === undefined || cam.channel === null) ? 0 : cam.channel;
  const user = cam.username || '';
  let detail = host + (port ? ':' + port : '') + ' · channel ' + channel +
    (user ? ' · ' + user : '') + ' · login kept on the server';
  if (cam.device_type === 'doorbell') detail += ' · doorbell';
  if (cam.two_way_talk) detail += ' · two-way talk capable';
  div.innerHTML = `
    <input type="hidden" class="sd-cam-source" value="reolink">
    <input type="hidden" class="sd-cam-host">
    <input type="hidden" class="sd-cam-port">
    <input type="hidden" class="sd-cam-channel">
    <input type="hidden" class="sd-cam-username">
    <input type="hidden" class="sd-cam-quality">
    <input type="hidden" class="sd-cam-device-type">
    <input type="hidden" class="sd-cam-two-way-talk">
    <div class="col-md-3">
      <label class="form-label small mb-1">Name</label>
      <input type="text" class="form-control form-control-sm sd-cam-name" placeholder="Front door">
    </div>
    <div class="col-md-8">
      <label class="form-label small mb-1">Reolink camera</label>
      <div class="form-control form-control-sm bg-body-tertiary text-secondary"><i class="bi bi-camera-video me-1"></i><span class="sd-cam-detail"></span></div>
    </div>
    <div class="col-md-1 d-flex gap-1">
      <button type="button" class="btn btn-outline-danger btn-sm" title="Remove" onclick="this.closest('.sd-camera-row').remove()"><i class="bi bi-trash"></i></button>
    </div>` + _camPopupTypesHtml();
  div.querySelector('.sd-cam-host').value = host;
  div.querySelector('.sd-cam-port').value = port;
  div.querySelector('.sd-cam-channel').value = channel;
  div.querySelector('.sd-cam-username').value = user;
  div.querySelector('.sd-cam-quality').value = cam.stream_quality || 'main';
  div.querySelector('.sd-cam-device-type').value = cam.device_type || '';
  div.querySelector('.sd-cam-two-way-talk').value = cam.two_way_talk ? '1' : '';
  div.querySelector('.sd-cam-name').value = cam.name || '';
  div.querySelector('.sd-cam-detail').textContent = detail;
  _camSetPopupTypes(div, cam.popup_types);
  wrap.appendChild(div);
}

// Save the Home Assistant URL and token to app settings (the server's source of
// truth, pulled by satellites). A blank token keeps the stored one. After saving,
// nudge the deck config so the credentials reach a connected deck right away.
async function savePaneHomeAssistant(btn) {
  const el = document.getElementById('ha-result');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving...'; }
  if (el) el.innerHTML = '';
  const base = document.getElementById('streamdeck_ha_base_url')?.value.trim() || '';
  const token = document.getElementById('streamdeck_ha_token')?.value || '';
  try {
    const body = {streamdeck_ha_base_url: base};
    // Only send the token when the user typed one; blank means keep the stored
    // secret (the field is never pre-filled with it).
    if (token.trim()) body.streamdeck_ha_token = token.trim();
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    try {
      let cfg = {};
      const cur = await fetch('setup/streamdeck/config').then(x => x.json());
      if (cur && cur.ok && cur.config && typeof cur.config === 'object') cfg = cur.config;
      const r = await fetch('setup/streamdeck/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({config: cfg}),
      });
      if (r.ok) await fetch('setup/streamdeck/restart', {method: 'POST'});
    } catch (e) { /* no bridge: settings are still saved on the server */ }
    // Clear the token box so the typed secret is not left on screen.
    const tokEl = document.getElementById('streamdeck_ha_token');
    if (tokEl) { tokEl.value = ''; tokEl.placeholder = '(unchanged)'; }
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Home Assistant saved.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Save the on-screen HA event channel settings (notification toasts + camera
// pop-ups). A reload picks up the new enabled state so the page starts (or
// stops) polling.
async function savePaneNotifications(btn) {
  const el = document.getElementById('ha-events-result');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving...'; }
  if (el) el.innerHTML = '';
  const enabled = document.getElementById('ha_events_enabled')?.checked || false;
  const seconds = parseInt(document.getElementById('ha_camera_popup_seconds')?.value || '20', 10) || 20;
  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ha_events_enabled: enabled, ha_camera_popup_seconds: seconds}),
    });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved. Reloading to apply...</span>';
    setTimeout(() => location.reload(), 700);
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Fire a sample notification through the event channel. If this page is polling
// (the channel is enabled and saved), the toast appears within a few seconds.
// Fill this device's real URL into the HA setup snippets (FoodAssistant-gaw9),
// so the user does not have to guess the address; window.location.origin is by
// definition an address that reaches this device.
function _haFillSnippetUrls() {
  const origin = window.location.origin.replace(/\/+$/, '');
  ['ha-snippet-rest', 'ha-snippet-auto'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.textContent.includes('http://THIS-DEVICE:9284')) {
      el.textContent = el.textContent.split('http://THIS-DEVICE:9284').join(origin);
    }
  });
}
document.addEventListener('DOMContentLoaded', _haFillSnippetUrls);

function _haCopySnippet(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-check2"></i> Copied';
    setTimeout(() => { btn.innerHTML = orig; }, 1500);
  }).catch(() => {});
}

async function _haSendTestEvent(btn) {
  const el = document.getElementById('ha-events-result');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Sending...'; }
  try {
    const r = await fetch('events/test', {method: 'POST'});
    const enabled = document.getElementById('ha_events_enabled')?.checked || false;
    if (el) el.innerHTML = enabled
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Sent. Watch for the toast in the top-right.</span>'
      : '<span class="text-warning">Sent, but turn the channel on and Save first to see it on screen.</span>';
    await r.json().catch(() => {});
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Probe Home Assistant with the current (saved or freshly typed) credentials and
// report how many camera entities it sees. Doubles as a connectivity check.
async function _haTestConnection(btn) {
  const el = document.getElementById('ha-result');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing...'; }
  if (el) el.innerHTML = '';
  const badge = document.getElementById('ha-conn-status');
  try {
    const data = await _haDiscover();
    if (data && data.ok) {
      const n = (data.cameras || []).length;
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Connected. ${n} camera${n === 1 ? '' : 's'} found.</span>`;
      if (badge) { badge.className = 'badge bg-success ms-2'; badge.innerHTML = '<i class="bi bi-check-circle me-1"></i>Connected'; }
    } else {
      if (el) el.innerHTML = `<span class="text-danger">${(data && data.error) || 'Could not connect.'}</span>`;
      if (badge) { badge.className = 'badge bg-danger ms-2'; badge.textContent = 'Not reachable'; }
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
    if (badge) { badge.className = 'badge bg-danger ms-2'; badge.textContent = 'Not reachable'; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Per-device on-screen events (FoodAssistant-vcuz): persist this device's choice
// in localStorage so the HA event poller (ha-events.js) can honour it.
function _haSetDeviceEvents(val) {
  try {
    if (val === 'default') localStorage.removeItem('haEventsShow');
    else localStorage.setItem('haEventsShow', val === '1' ? '1' : '0');
  } catch (e) { }
}
function _haInitDeviceEvents() {
  const sel = document.getElementById('ha_events_device');
  if (!sel) return;
  let v = null;
  try { v = localStorage.getItem('haEventsShow'); } catch (e) { }
  sel.value = (v === '1' || v === '0') ? v : 'default';
}
