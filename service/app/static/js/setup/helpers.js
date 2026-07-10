// Shared helpers
function val(id) { return document.getElementById(id)?.value.trim() || ''; }

// Where the "Open Grocy" button should point. A localhost/Docker API base is
// not reachable from the browser, so prefer the server-computed browser link
// (<hostname>.local or LAN IP); otherwise use the entered URL as-is. On a
// satellite the server-computed link always wins: it resolves to the main
// server's LAN address rather than an address local to that server.
function _grocyOpenHref() {
  const entered = val('grocy_base_url');
  const isLocal = /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|grocy)(:|\/|$)/i.test(entered);
  if ((IS_SATELLITE || !entered || isLocal) && GROCY_BROWSER_LINK) return GROCY_BROWSER_LINK;
  return entered;
}
function _updateGrocyOpenLink() {
  const link = document.getElementById('grocy-open-link');
  if (link) {
    const href = _grocyOpenHref();
    if (href) link.href = href;
  }
}
function num(id, fallback) { const v = parseInt(val(id), 10); return isNaN(v) ? fallback : v; }

// The kitchen name becomes part of the Forager web address (a subdomain), so
// only letters, numbers, and dashes survive there. Sanitize the field as the
// user types so a friendly name like "Dan's Kitchen" turns into "dans-kitchen"
// instead of tripping up sign-in. Gentle by design: it lowercases, turns runs
// of spaces or underscores into single dashes, and drops anything else, but it
// never blocks typing and leaves a trailing dash alone mid-word.
function _slugKitchenName(raw) {
  return String(raw || '')
    .toLowerCase()
    .replace(/[\s_]+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-{2,}/g, '-');
}
function sanitizeKitchenName(el) {
  if (!el) return;
  const cleaned = _slugKitchenName(el.value);
  if (cleaned !== el.value) el.value = cleaned;
}

function secretVal(id) {
  const el = document.getElementById(id);
  if (!el) return '';
  if (el.dataset.clear === '1') return '__CLEAR__';
  return el.value.trim();
}

// GET a URL as JSON with a hard per-request timeout (AbortController). The
// setup wizard's watch loops (Mealie/Grocy install, log tailing) poll the host
// bridge, and a single request that never answers must not stall the loop
// forever: a timed-out fetch rejects, the caller counts the iteration, and the
// loop stays bounded. Throws on timeout, network error, or a non-JSON body.
async function _fetchJson(url, ms, opts) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms || 8000);
  try {
    const r = await fetch(url, Object.assign({ signal: ctrl.signal }, opts || {}));
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

// Append an empty additional-key row for a provider's fallback list.
function addExtraKey(provider) {
  const box = document.querySelector(`.extra-keys[data-provider="${provider}"]`);
  if (!box) return;
  const row = document.createElement('div');
  row.className = 'input-group mb-2 extra-key-row';
  row.innerHTML =
    '<input type="text" class="form-control font-monospace extra-key-input" ' +
    'placeholder="paste a spare API key">' +
    '<button class="btn btn-outline-danger" type="button" title="Remove key" ' +
    'onclick="this.closest(\'.extra-key-row\').remove()"><i class="bi bi-trash3"></i></button>';
  box.appendChild(row);
}

// Generate a new satellite key and add it as a plaintext row so the user can
// immediately copy it before saving.
function addSatelliteKey() {
  const hex = randomHex(24);
  const box = document.querySelector('.satellite-extra-keys');
  if (!box) return;
  // The API keys panel renders as a table (FoodAssistant-jcnh), so a new key is
  // appended as a table row matching the Name / Key / Type / actions columns.
  const row = document.createElement('tr');
  row.className = 'satellite-key-row';
  row.innerHTML =
    '<td><input type="text" class="form-control form-control-sm satellite-key-name" placeholder="name (optional)"></td>' +
    '<td><input type="text" class="form-control form-control-sm font-monospace satellite-key-input" value="' + hex + '"></td>' +
    '<td><span class="set-pill set-pill-neutral">Satellite</span></td>' +
    '<td class="set-keys-actions">' +
    '<button class="btn btn-outline-secondary btn-sm me-1" type="button" title="Copy this key">' +
    '<i class="bi bi-clipboard"></i></button>' +
    '<button class="btn btn-outline-danger btn-sm" type="button" title="Remove this key" ' +
    'onclick="this.closest(\'.satellite-key-row\').remove()"><i class="bi bi-trash3"></i></button>' +
    '</td>';
  const copyBtn = row.querySelector('button.btn-outline-secondary');
  copyBtn.onclick = function () { copyText(hex, this); };
  box.appendChild(row);
}

// Reveal a hidden secret-change block (the password Change / Set flows in the
// Security pane) and hide the button that opened it, then focus the first
// field. Keeps the change-password fields behind an explicit action rather than
// always visible (FoodAssistant-jcnh).
function revealSecretChange(targetId, btn) {
  const box = document.getElementById(targetId);
  if (!box) return;
  box.classList.remove('d-none');
  if (btn) btn.classList.add('d-none');
  const inp = box.querySelector('input');
  if (inp) inp.focus();
}

// Each row becomes {key, name}; the merge resolves __KEEP__ placeholders and
// pairs the (possibly edited) name with its key.
function collectSatelliteKeys() {
  return [...document.querySelectorAll('.satellite-key-row')]
    .map(row => {
      const key = (row.querySelector('.satellite-key-input')?.value || '').trim();
      const name = (row.querySelector('.satellite-key-name')?.value || '').trim();
      return { key, name };
    })
    .filter(r => r.key);
}

// Build {provider: [keys]} from the additional-key rows. Untouched saved rows
// keep their __KEEP__:<index> placeholder so secrets are never echoed.
function collectExtraKeys() {
  const out = {};
  document.querySelectorAll('.extra-keys').forEach(box => {
    const provider = box.dataset.provider;
    const keys = [...box.querySelectorAll('.extra-key-input')]
      .map(i => i.value.trim()).filter(Boolean);
    if (keys.length) out[provider] = keys;
    else out[provider] = [];
  });
  return out;
}

function addCatRow() {
  const tpl = `<div class="cat-row border rounded p-2">
    <div class="row g-2 align-items-end">
      <div class="col-6 col-md-3">
        <label class="form-label small mb-1">Name</label>
        <input class="form-control form-control-sm cat-label" placeholder="Wine Cellar">
      </div>
      <div class="col-6 col-md-2">
        <label class="form-label small mb-1">Icon <a href="https://icons.getbootstrap.com/" target="_blank" class="text-secondary" title="Browse icons"><i class="bi bi-box-arrow-up-right"></i></a></label>
        <input class="form-control form-control-sm cat-icon" placeholder="bi-database" value="bi-box">
      </div>
      <div class="col-4 col-md-1">
        <label class="form-label small mb-1">Color</label>
        <input type="color" class="form-control form-control-sm form-control-color cat-color" value="#adb5bd">
      </div>
      <div class="col-8 col-md-3">
        <label class="form-label small mb-1">Grocy location</label>
        <input class="form-control form-control-sm cat-location" placeholder="Wine Cellar">
      </div>
      <div class="col-9 col-md-2">
        <label class="form-label small mb-1">Match <span class="text-secondary">keywords</span></label>
        <input class="form-control form-control-sm cat-match" placeholder="wine, cellar">
      </div>
      <div class="col-3 col-md-1 d-grid">
        <button class="btn btn-outline-danger btn-sm" onclick="this.closest('.cat-row').remove()" title="Remove"><i class="bi bi-trash"></i></button>
      </div>
    </div>
  </div>`;
  document.getElementById('storage-cat-editor').insertAdjacentHTML('beforeend', tpl);
}

async function saveStorageCategories() {
  const btn = document.getElementById('catSaveBtn');
  const result = document.getElementById('catSaveResult');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  result.textContent = '';
  const rows = [...document.querySelectorAll('#storage-cat-editor .cat-row')];
  const categories = rows.map(row => ({
    label:    row.querySelector('.cat-label').value.trim(),
    icon:     row.querySelector('.cat-icon').value.trim() || 'bi-box',
    color:    row.querySelector('.cat-color').value.trim(),
    bg:       '#2a2a33',
    location: row.querySelector('.cat-location').value.trim(),
    match:    row.querySelector('.cat-match').value.split(',').map(s => s.trim()).filter(Boolean),
  })).filter(c => c.label);
  try {
    const r = await fetch('setup/storage-categories', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({categories}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    result.className = 'small text-success';
    result.textContent = 'Saved';
    setTimeout(() => { result.textContent = ''; }, 3000);
  } catch(e) {
    result.className = 'small text-danger';
    result.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Save categories';
  }
}

function toggleAuthRequired() {
  const on = document.getElementById('auth_required').checked;
  const hint = document.getElementById('auth-pw-hint');
  if (hint) hint.classList.toggle('text-danger', on);
  // Keep the Authentication panel's On/Off state pill in sync (FoodAssistant-jcnh).
  const statePill = document.getElementById('auth-state-pill');
  if (statePill) {
    statePill.textContent = on ? 'On' : 'Off';
    statePill.classList.toggle('set-pill-good', on);
    statePill.classList.toggle('set-pill-neutral', !on);
  }
}

// The settings backup can carry API keys and passwords, so the download is a
// POST that re-confirms the current app password (FoodAssistant-16cj). We fetch
// the zip as a blob so a wrong password can show an inline error instead of
// dumping a JSON 403 into a downloaded file. An open install (no password
// field rendered) simply posts an empty password and the server streams it.
async function downloadBackup(btn) {
  const orig = btn.innerHTML;
  const result = document.getElementById('backup-result');
  const pwField = document.getElementById('backup_password');
  if (result) result.innerHTML = '';
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Preparing…';
  try {
    const fd = new FormData();
    fd.append('backup_password', pwField?.value || '');
    fd.append('include_secrets',
      document.getElementById('backup_include_secrets')?.checked ? 'true' : 'false');
    const r = await fetch('admin/backup', { method: 'POST', body: fd });
    if (!r.ok) {
      let msg = 'Download failed.';
      try { const d = await r.json(); if (d.detail) msg = d.detail; } catch (e) {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    let name = 'foodassistant-backup.zip';
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^"]+)"?/);
    if (m) name = m[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    if (pwField) pwField.value = '';
  } catch (e) {
    if (result) result.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle me-1"></i>'
      + e.message + '</span>';
  } finally {
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function checkUpdate(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking…';
  const el = document.getElementById('update-result');
  try {
    const r = await fetch('admin/check-update');
    const d = await r.json();
    if (!d.ok) {
      el.innerHTML = '<span class="text-secondary"><i class="bi bi-dash-circle me-1"></i>' +
        (d.error || 'Update check unavailable') + ' (running v' + d.current + ').</span>';
    } else if (d.update_available) {
      el.innerHTML = '<span class="text-warning"><i class="bi bi-exclamation-circle-fill me-1"></i>' +
        'Update available: <strong>' + d.latest + '</strong> (you have v' + d.current + '). ' +
        '<a href="' + (d.release_url || '#') + '" target="_blank">View on GitHub</a>, then run a command below.</span>';
    } else {
      el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' +
        'You are on the latest version (v' + d.current + ').</span>';
    }
  } catch (e) {
    el.innerHTML = '<span class="text-secondary">Update check failed.</span>';
  }
  btn.disabled = false;
  btn.innerHTML = orig;
}

function randomHex(n) {
  const arr = new Uint8Array(n);
  crypto.getRandomValues(arr);
  return Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
}

// Copy text to the clipboard with a non-secure-context fallback (the kiosk and
// LAN browsers run over plain HTTP, where navigator.clipboard is unavailable).
async function copyText(text, btn) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(text);
    ok = true;
  } catch (e) {
    const t = document.createElement('textarea');
    t.value = text;
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.appendChild(t);
    t.select();
    try { ok = document.execCommand('copy'); } catch (_) {}
    t.remove();
  }
  if (btn) {
    const old = btn.innerHTML;
    btn.innerHTML = ok ? '<i class="bi bi-check-lg"></i>' : '<i class="bi bi-x-lg"></i>';
    setTimeout(() => { btn.innerHTML = old; }, 1200);
  }
  return ok;
}

// Show a "copy it now" note with a Copy button right under a freshly generated
// secret. The note is rebuilt idempotently so repeated Generate clicks update it.
function showGeneratedSecret(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const group = el.closest('.input-group') || el.parentElement;
  let note = document.getElementById(id + '-gennote');
  if (!note) {
    note = document.createElement('div');
    note.id = id + '-gennote';
    note.className = 'alert alert-warning d-flex align-items-center gap-2 py-2 mt-2 mb-0 small';
    group.parentElement.insertBefore(note, group.nextSibling);
  }
  note.innerHTML =
    '<i class="bi bi-exclamation-triangle-fill"></i>' +
    '<span class="flex-grow-1">New key generated. <strong>Copy it now</strong>: it is hidden once you save.</span>' +
    '<button class="btn btn-sm btn-outline-dark" type="button" title="Copy key"><i class="bi bi-clipboard me-1"></i>Copy</button>';
  note.querySelector('button').onclick = function () { copyText(value, this); };
}

function generateApiKey() {
  const hex = randomHex(24);
  // Wizard and settings share the same input id; only one is rendered at a time.
  const el = document.getElementById('api_key');
  if (!el) return;
  el.value = hex;
  el.type = 'text';
  el.dataset.clear = '';
  el.disabled = false;
  showGeneratedSecret('api_key', hex);
}

function clearSecret(id) {
  const el = document.getElementById(id);
  if (el.dataset.clear === '1') {
    el.dataset.clear = '';
    el.disabled = false;
    el.placeholder = '•••• saved, leave blank to keep';
  } else {
    el.dataset.clear = '1';
    el.value = '';
    el.disabled = true;
    el.placeholder = 'will be erased on save';
  }
}

function toggleVis(id) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
}

function setResult(id, ok, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = ok
    ? `<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>${msg}</span>`
    : `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${msg}</span>`;
}

function showProvider() {
  const p = val('vision_provider') || document.getElementById('vision_provider')?.value;
  document.querySelectorAll('.provider-fields').forEach(d => {
    d.style.display = 'none';
    d.classList.add('d-none');
  });
  const target = document.getElementById('fields-' + p);
  if (target) {
    target.style.display = '';
    target.classList.remove('d-none');
  }
  // Forager (stored as "cloud") picks and runs the model itself and holds no
  // provider key here, so there is no fields-cloud block and the direct-provider
  // model/key controls stay hidden. Show the managed-model note in their place;
  // hide it for every direct provider (FoodAssistant-rgwa).
  const foragerNote = document.getElementById('forager-managed-note');
  if (foragerNote) foragerNote.classList.toggle('d-none', p !== 'cloud');
  // The enrichment override follows the vision provider when it is left on
  // "same as vision provider", so re-sync its Forager gate too.
  if (typeof syncEnrichForagerGate === 'function') syncEnrichForagerGate();
}

// Provider-dependent model picker -------------------------------------------

// Sync the note + custom-input visibility after the dropdown changes.
function aiModelSelChanged(provider) {
  const sel = document.getElementById(provider + '_model_sel');
  const input = document.getElementById(provider + '_model');
  const note = document.getElementById(provider + '_model_note');
  if (!sel || !input) return;
  if (sel.value === '__custom__') {
    input.classList.remove('d-none');
    if (note) note.textContent = 'Enter any vision-capable model id this provider accepts.';
    if (!input.value) input.focus();
  } else {
    input.value = sel.value;            // the value buildPayload reads
    input.classList.add('d-none');
    const m = (AI_MODELS[provider] || []).find(x => x.id === sel.value);
    if (note) note.textContent = m ? m.note : '';
  }
}

// On load, point each provider's dropdown at the stored model (or Custom when
// the stored model is not in the curated list), and show the matching note.
function initAiModelPickers() {
  Object.keys(AI_MODELS).forEach(provider => {
    const sel = document.getElementById(provider + '_model_sel');
    const input = document.getElementById(provider + '_model');
    if (!sel || !input) return;
    const current = (input.value || '').trim();
    const known = (AI_MODELS[provider] || []).some(x => x.id === current);
    if (current && !known) {
      sel.value = '__custom__';
    } else if (current) {
      sel.value = current;
    }
    aiModelSelChanged(provider);
  });
}

// Barcode enrichment model picker (FoodAssistant-r4nz). Mirrors the main AI
// model dropdown but its options follow the enrichment provider (or the vision
// provider when "Same as vision provider" is chosen). The hidden #enrich_model
// input holds the effective value the save path reads: empty (use the
// provider's default), a curated id, or a custom id.
function _enrichEffectiveProvider() {
  return (document.getElementById('enrich_provider')?.value)
    || document.getElementById('vision_provider')?.value
    || 'gemini';
}

// Forager (stored as "cloud") manages its own model, so a model override is
// meaningless and would be ignored. When the effective enrichment provider is
// Forager, hide the model-override row and show the managed-model note in its
// place; a direct provider gets the override back (FoodAssistant-rgwa).
function syncEnrichForagerGate() {
  const row = document.getElementById('enrich-model-row');
  const note = document.getElementById('enrich-forager-note');
  const isForager = _enrichEffectiveProvider() === 'cloud';
  if (row) row.classList.toggle('d-none', isForager);
  if (note) note.classList.toggle('d-none', !isForager);
}

function populateEnrichModelOptions() {
  const sel = document.getElementById('enrich_model_sel');
  if (!sel) return;
  const provider = _enrichEffectiveProvider();
  const models = AI_MODELS[provider] || [];
  let html = '<option value="">Same as the provider\'s model</option>';
  models.forEach(m => { html += `<option value="${m.id}">${m.id}: ${m.note}</option>`; });
  html += '<option value="__custom__">Custom model…</option>';
  sel.innerHTML = html;
}

// Reflect the dropdown choice into the hidden #enrich_model input + note.
function enrichModelSelChanged() {
  const sel = document.getElementById('enrich_model_sel');
  const input = document.getElementById('enrich_model');
  const note = document.getElementById('enrich_model_note');
  if (!sel || !input) return;
  if (sel.value === '__custom__') {
    input.classList.remove('d-none');
    if (note) note.textContent = 'Enter any model id this provider accepts (text-only, so a small model is fine).';
    if (!input.value) input.focus();
  } else if (sel.value === '') {
    input.value = '';
    input.classList.add('d-none');
    if (note) note.textContent = 'Empty uses the enrichment provider\'s default model above.';
  } else {
    input.value = sel.value;
    input.classList.add('d-none');
    const m = (AI_MODELS[_enrichEffectiveProvider()] || []).find(x => x.id === sel.value);
    if (note) note.textContent = m ? m.note : '';
  }
}

// Point the dropdown at the stored enrich_model (Custom when it is not in the
// curated list for the effective provider), and show the matching note.
function initEnrichModelPicker() {
  const sel = document.getElementById('enrich_model_sel');
  const input = document.getElementById('enrich_model');
  if (!sel || !input) return;
  populateEnrichModelOptions();
  const current = (input.value || '').trim();
  const known = (AI_MODELS[_enrichEffectiveProvider()] || []).some(x => x.id === current);
  if (!current) {
    sel.value = '';
  } else if (known) {
    sel.value = current;
  } else {
    sel.value = '__custom__';
  }
  enrichModelSelChanged();
  syncEnrichForagerGate();
}

// When the enrichment provider changes, rebuild the option list. Keep the
// stored model if it is still valid for the new provider; otherwise fall back
// to the provider default (empty) so we never show a model from another vendor.
function onEnrichProviderChange() {
  const sel = document.getElementById('enrich_model_sel');
  const input = document.getElementById('enrich_model');
  if (!sel || !input) return;
  const current = (input.value || '').trim();
  populateEnrichModelOptions();
  const known = (AI_MODELS[_enrichEffectiveProvider()] || []).some(x => x.id === current);
  if (current && known) {
    sel.value = current;
  } else if (current) {
    sel.value = '__custom__';
  } else {
    sel.value = '';
  }
  enrichModelSelChanged();
  syncEnrichForagerGate();
}

function showRecipeSource() {
  const src = document.getElementById('recipe_source')?.value;
  const td = document.getElementById('source-themealdb');
  const sd = document.getElementById('source-spoonacular');
  if (td) td.style.display = src === 'themealdb' ? '' : 'none';
  if (sd) sd.style.display = src === 'spoonacular' ? '' : 'none';
}

function showRecipeSourceWiz() {
  const src = document.getElementById('recipe_source_wiz')?.value;
  const sd = document.getElementById('wiz-source-spoonacular');
  if (sd) sd.classList.toggle('d-none', src !== 'spoonacular');
}

// Connection tests
async function postJson(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  return r.json();
}

// Live install/start log tailing (FoodAssistant-59z).
// Collapse the flood of Docker image-pull progress into a readable view
// (FoodAssistant-n5ky). Without a terminal, `docker compose up` emits a new
// line for every byte of every layer's Downloading/Extracting progress, so a
// normal-but-slow pull on a Pi SD card looks like hundreds of near-identical
// lines. This turns that into: the real log messages, plus one live status
// line per image summarizing how many layers are done and what is happening
// now. Pure function (no DOM), so it is unit-testable.
function _cleanInstallLog(lines) {
  // Per-layer progress words that repeat by the hundred: dropped from the body
  // and summarized instead. Image-level Pulling/Pulled lines are kept as
  // milestones.
  var NOISE = /\b(Pulling fs layer|Waiting|Downloading|Verifying Checksum|Download complete|Extracting|Pull complete|Already exists)\b/;
  var DONE = /\b(Pull complete|Already exists)\b/;
  var ACTIVE = /\b(Downloading|Extracting)\b/;
  var isLayer = function (tok) { return /^[0-9a-f]{12,}$/.test(tok); };

  // image -> {layers:Set, done:Set, phase:string}
  var images = {};
  var current = null;   // the image whose layers we are currently counting
  var body = [];
  for (var i = 0; i < lines.length; i++) {
    var raw = lines[i];
    var m = raw.match(/^\s*(\S+)\s+(.*)$/);
    var tok = m ? m[1] : '';
    var rest = m ? m[2] : '';
    if (tok && !isLayer(tok) && /^Pulling\b/.test(rest)) {
      // "grocy Pulling" starts an image; make it the current counting target.
      if (!images[tok]) images[tok] = {layers: {}, done: {}, phase: '', pulled: false};
      current = tok;
      body.push(raw.trim());
      continue;
    }
    if (tok && !isLayer(tok) && /^Pulled\b/.test(rest)) {
      if (images[tok]) images[tok].pulled = true;
      body.push(raw.trim());
      continue;
    }
    if (isLayer(tok) && current && images[current]) {
      var img = images[current];
      img.layers[tok] = true;
      if (DONE.test(rest)) img.done[tok] = true;
      if (ACTIVE.test(rest)) img.phase = /Downloading/.test(rest) ? 'downloading' : 'extracting';
      continue;   // per-layer noise: summarized, not shown
    }
    if (NOISE.test(raw)) continue;   // stray progress line without a known image
    body.push(raw);                  // a real message, error, or echo: keep it
  }

  var status = [];
  Object.keys(images).forEach(function (name) {
    var img = images[name];
    var total = Object.keys(img.layers).length;
    var done = Object.keys(img.done).length;
    if (img.pulled) {
      status.push(name + ': downloaded');
    } else if (total) {
      status.push(name + ': ' + done + '/' + total + ' layers' +
                  (img.phase ? ' (' + img.phase + ')' : ''));
    } else {
      status.push(name + ': starting');
    }
  });
  return {body: body, status: status};
}

// Each install action has one or more <pre class="install-log" data-log="NAME">
// panels (there can be two, one per setup view). Fetch the bridge log tail and
// render it into every matching panel, auto-scrolling to the newest line.
async function _refreshInstallLog(name) {
  const panels = document.querySelectorAll('.install-log[data-log="' + name + '"]');
  if (!panels.length) return {ok: false};
  let data;
  try {
    data = await _fetchJson('setup/logs/' + name, 8000);
  } catch (e) {
    return {ok: false};
  }
  const lines = (data && Array.isArray(data.lines)) ? data.lines : [];
  let text;
  if (lines.length) {
    const clean = _cleanInstallLog(lines);
    // Keep the tail of real messages readable, then the live pull summary so
    // the newest, most useful state sits at the bottom by the auto-scroll.
    const body = clean.body.slice(-40).join('\n');
    const summary = clean.status.length
      ? '\nPulling images (this is one-time and slower on an SD card):\n  ' +
        clean.status.join('\n  ')
      : '';
    text = (body + summary).trim() || '(waiting for output…)';
  } else {
    text = '(waiting for output…)';
  }
  panels.forEach(p => {
    p.classList.remove('d-none');
    const atBottom = p.scrollTop + p.clientHeight >= p.scrollHeight - 4;
    p.textContent = text;
    if (atBottom) p.scrollTop = p.scrollHeight;
  });
  return data || {ok: false};
}

// Poll the log every ~1.5s while a step is in flight. `isDone()` returns true
// once the caller's own status poll says the work finished, which stops the
// log loop after one final refresh so the last output stays on screen.
function _startLogPolling(name, isDone) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    await _refreshInstallLog(name);
    if (isDone()) {
      await _refreshInstallLog(name);
      return;
    }
    setTimeout(tick, 1500);
  };
  tick();
  return () => { stopped = true; };
}

async function testGrocy() {
  setResult('grocy-result', true, 'Testing…');
  const d = await postJson('setup/test/grocy',
    {grocy_base_url: val('grocy_base_url'), grocy_api_key: secretVal('grocy_api_key')});
  setResult('grocy-result', d.ok, d.ok ? d.message : d.error);
}

// Reveal/enable every "Open Mealie" link once Mealie actually serves HTTP.
function _enableMealieLinks() {
  ['wiz-mealie-open', 'mealie-open-link', 'mealie-url-open'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.classList.remove('disabled'); el.removeAttribute('aria-disabled'); }
  });
  document.querySelectorAll('#mealie-token-hint').forEach(el => el.classList.remove('d-none'));
}

const _sleep = ms => new Promise(r => setTimeout(r, ms));

// Once a just-started Mealie serves HTTP, ask the server to connect it
// automatically (FoodAssistant-syxf): it creates the API token, secures the
// sign-in, and adds a Groceries shopping list, so no manual token dance is
// needed. Idempotent: the server-side start hook may already have done it,
// in which case this simply reports the connected state. When Mealie turns
// out to have its own sign-in already, the manual token instructions stay.
async function _mealieAutoConnect() {
  let d;
  try {
    d = await postJson('setup/first-run/mealie',
      {base_url: val('mealie_base_url') || 'http://localhost:9285'});
  } catch (e) {
    d = null;
  }
  if (d && d.ok && d.configured) {
    setResult('mealie-start-result', true,
      d.message || 'Mealie is connected and ready to use.');
    // The manual token instructions no longer apply; hide them. No reload:
    // in the wizard that would throw the user back to the first step.
    document.querySelectorAll('#mealie-token-hint').forEach(el => el.classList.add('d-none'));
    return;
  }
  setResult('mealie-start-result', true,
    'Mealie is running. Open it to create an API token, then paste it below.');
}

// The Grocy twin of _mealieAutoConnect, used by the wizard's appliance
// install watch: once the local Grocy serves HTTP, ask the server to
// connect it automatically. Falls back to the manual API key instructions
// when Grocy turns out to have its own sign-in already.
async function _grocyAutoConnect() {
  let d;
  try {
    d = await postJson('setup/first-run/grocy', {base_url: val('grocy_base_url')});
  } catch (e) {
    d = null;
  }
  if (d && d.ok && d.configured) {
    window._firstRunGrocyDone = true;
    setResult('grocy-install-result', true,
      d.message || 'Grocy is connected and ready to use.');
    return;
  }
  setResult('grocy-install-result', true,
    'Grocy is up. Open it to create an API key, then paste it below.');
}

// Poll Mealie's status until it actually answers HTTP before claiming
// success. The bridge runs the image pull/up in the background, so on first
// run this can take a few minutes; we keep a progress state and live log
// visible until state == "running", then reveal the Open Mealie link
// (FoodAssistant-28z + 59z). Shared by startMealie and the page-load
// re-attach below (FoodAssistant-nqpb).
// Single-flight guard: startMealie and the page-load resume can both reach
// here, and a stacked second loop would double the polling and could re-disable
// a button the other loop already released. Only one Mealie watch runs at a time.
let _mealieWatchActive = false;
async function _watchMealie() {
  if (_mealieWatchActive) return;
  _mealieWatchActive = true;
  let running = false;
  const stopLog = _startLogPolling('mealie', () => running);
  try {
    // Poll status every ~2s. Mealie reports "running" only once it serves HTTP.
    // Each probe has a hard timeout, so one request that never answers cannot
    // freeze this loop (and the caller's spinner) forever: it counts as a miss
    // and the bounded loop moves on.
    for (let i = 0; i < 150 && !running; i++) {
      await _sleep(2000);
      let s;
      try {
        s = await _fetchJson('setup/mealie/status', 8000);
      } catch (e) {
        continue;
      }
      if (!s || s.ok === false) continue;
      if (s.state === 'running') {
        running = true;
        setResult('mealie-start-result', true, 'Mealie is running. Connecting it to Pantry Raider…');
        _enableMealieLinks();
        await _mealieAutoConnect();
      } else if (s.state === 'not-installed') {
        running = true;
        setResult('mealie-start-result', false, 'Mealie stopped before it started serving. Check the log above and try again.');
      }
      // state === "starting": keep waiting, the log panel shows progress.
    }
    if (!running) {
      running = true;
      setResult('mealie-start-result', false, 'Mealie is taking longer than expected. Check the log above; it may still come up.');
    }
  } finally {
    running = true;
    stopLog();
    _mealieWatchActive = false;
  }
}

async function startMealie(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Starting...';
  try {
    const d = await _fetchJson('setup/mealie/start', 15000,
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (!d.ok) {
      setResult('mealie-start-result', false, d.error || 'Failed to start Mealie.');
      return;
    }
    if (d.state === 'running') {
      setResult('mealie-start-result', true, 'Mealie is running. Connecting it to Pantry Raider…');
      _enableMealieLinks();
      await _mealieAutoConnect();
      return;
    }
    setResult('mealie-start-result', true, 'Starting Mealie (the first start downloads it and can take a few minutes)…');
    await _watchMealie();
  } catch(e) {
    setResult('mealie-start-result', false, e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// If a Mealie start is already running (the page was left and reopened
// mid-download), pick it back up: show the progress state and log again
// instead of sitting idle until the button is clicked a second time.
async function resumeMealieWatch() {
  if (!document.querySelector('.install-log[data-log="mealie"]')) return;
  let s;
  try {
    s = await _fetchJson('setup/mealie/status', 8000);
  } catch (e) {
    return;
  }
  if (!s || s.ok === false || s.state !== 'starting') return;
  setResult('mealie-start-result', true, 'Mealie is still starting from an earlier request; reconnected to its progress…');
  await _watchMealie();
}
document.addEventListener('DOMContentLoaded', resumeMealieWatch);

async function testMealie() {
  setResult('mealie-result', true, 'Testing…');
  const d = await postJson('setup/test/mealie',
    {mealie_base_url: val('mealie_base_url'), mealie_api_key: secretVal('mealie_api_key')});
  setResult('mealie-result', d.ok, d.ok ? d.message : d.error);
}

// Zero-touch first-run provisioning (FoodAssistant-syxf): "Set up for me"
// buttons in the Grocy and Mealie panes. The server signs in with the
// backend's factory sign-in, creates the API key/token itself, secures the
// account, and saves everything; the page then reloads to show the connected
// state. A backend that is already someone's (key saved, or password
// changed) is reported and left untouched.
async function firstRunProvision(service, btn, resultId, baseUrl, reload = true) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Setting up…';
  try {
    const d = await postJson('setup/first-run/' + service, {base_url: baseUrl || ''});
    setResult(resultId, !!d.ok, d.message || d.error || 'Something went wrong.');
    if (d.ok && d.configured) {
      if (service === 'grocy') window._firstRunGrocyDone = true;
      // Reload to show the connected state, except in the wizard, where a
      // reload would throw the user back to the first step.
      if (reload) setTimeout(() => location.reload(), 1600);
    }
  } catch (e) {
    setResult(resultId, false, e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

function firstRunGrocy(btn) {
  return firstRunProvision('grocy', btn, 'grocy-firstrun-result', val('grocy_base_url'));
}

function firstRunMealie(btn) {
  return firstRunProvision('mealie', btn, 'mealie-firstrun-result', val('mealie_base_url'));
}

// Reveal the generated backend sign-in stored when Pantry Raider set the
// backend up itself. Fetched on click only, never rendered into the page.
async function revealFirstRunLogin(service, resultId) {
  const d = await postJson('setup/first-run/reveal', {service: service});
  const el = document.getElementById(resultId);
  if (!el) return;
  if (d.ok) {
    el.innerHTML = '';
    const wrap = document.createElement('span');
    wrap.className = 'font-monospace small';
    wrap.textContent = 'Username: ' + d.username + '   Password: ' + d.password;
    el.appendChild(wrap);
  } else {
    setResult(resultId, false, d.error || 'No saved sign-in.');
  }
}

function scannerTypeChanged() {
  var v = val('scanner_type');
  document.getElementById('scanner-usb-hint').classList.toggle('d-none', v !== 'usb');
  document.getElementById('scanner-camera-hint').classList.toggle('d-none', v !== 'camera');
  if (v !== 'usb') {
    var inp = document.getElementById('scanner-test-input');
    if (inp) inp.value = '';
    setResult('scanner-test-result', true, '');
  }
}

var _scannerIdleTimer = null;

function scannerTestInput(v) {
  if (v.length < 6) setResult('scanner-test-result', true, '');
  // Not every scanner is configured to send a trailing Enter/CR. Confirm on a
  // short idle once enough characters have arrived, so a wedge that just types
  // the digits still passes the test without an explicit Enter suffix.
  if (_scannerIdleTimer) clearTimeout(_scannerIdleTimer);
  _scannerIdleTimer = setTimeout(function () { scannerTestEnter(v); }, 250);
}

function scannerTestEnter(v) {
  if (_scannerIdleTimer) { clearTimeout(_scannerIdleTimer); _scannerIdleTimer = null; }
  v = v.trim();
  if (!v) return;
  if (/^[\x20-\x7E]{6,}$/.test(v)) {
    setResult('scanner-test-result', true, 'Barcode received: ' + v);
  } else {
    setResult('scanner-test-result', false, 'Input looks unexpected: check scanner is in HID mode.');
  }
}

function wizScannerTypeChanged() {
  var v = document.getElementById('wiz-scanner_type').value;
  document.getElementById('wiz-scanner-usb-hint').classList.toggle('d-none', v !== 'usb');
  document.getElementById('wiz-scanner-camera-hint').classList.toggle('d-none', v !== 'camera');
  if (v !== 'usb') {
    var inp = document.getElementById('wiz-scanner-test-input');
    if (inp) inp.value = '';
    setResult('wiz-scanner-test-result', true, '');
  }
}

function wizScannerTestInput(v) {
  if (v.length < 6) setResult('wiz-scanner-test-result', true, '');
}

function wizScannerTestEnter(v) {
  v = v.trim();
  if (!v) return;
  if (/^[\x20-\x7E]{6,}$/.test(v)) {
    setResult('wiz-scanner-test-result', true, 'Barcode received: ' + v);
  } else {
    setResult('wiz-scanner-test-result', false, 'Input looks unexpected: check scanner is in HID mode.');
  }
}

function providerTestPayload(provider, modelOverride) {
  return {
    provider,
    api_key: secretVal(provider + '_api_key'),
    model: modelOverride || val(provider + '_model'),
    base_url: val('ollama_base_url'),
  };
}

async function testVision() {
  setResult('vision-result', true, 'Testing…');
  const p = document.getElementById('vision_provider').value;
  if (p === 'none') { setResult('vision-result', false, 'No provider selected.'); return; }
  const d = await postJson('setup/test/provider', providerTestPayload(p, ''));
  setResult('vision-result', d.ok, d.ok ? d.message : d.error);
}

async function testEnrich() {
  setResult('enrich-result', true, 'Testing…');
  const provider = val('enrich_provider') || document.getElementById('vision_provider').value;
  const d = await postJson('setup/test/provider', providerTestPayload(provider, val('enrich_model')));
  setResult('enrich-result', d.ok, d.ok ? d.message : d.error);
}

async function testRecipes() {
  setResult('recipes-result', true, 'Testing…');
  const src = document.getElementById('recipe_source').value;
  const d = await postJson('setup/test/recipes',
    {source: src, api_key: secretVal(src === 'spoonacular' ? 'spoonacular_api_key' : 'themealdb_api_key')});
  setResult('recipes-result', d.ok, d.ok ? d.message : d.error);
}
