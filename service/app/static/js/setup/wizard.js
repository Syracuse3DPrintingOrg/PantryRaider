// 
// WIZARD logic (first-time setup only)
// 

let _wizStep = 1;
const _WIZ_TOTAL = 7;
// Deployment mode: 'server' | 'pi_hosted' | 'pi_remote'. Drives Grocy URL
// pre-fill and which steps the wizard shows.
let _installMode = SETUP_CURRENT_MODE;

// Steps shown for each mode.
// Hardware (3) is shown only on Pi modes (hosted/remote). Server installs
// skip it since there is no attached display or Stream Deck.
// Pi Remote skips Grocy (4), AI (5), and Optional (6): just
// mode, security, hardware, and done.
function _wizStepSeq() {
  if (_installMode === 'pi_remote') return [1, 2, 3, 7];
  if (_installMode === 'pi_hosted') return [1, 2, 3, 4, 5, 6, 7];
  return [1, 2, 4, 5, 6, 7];  // server: skip Hardware
}

function selectInstallMode(mode) {
  _installMode = mode;
  document.querySelectorAll('.install-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('mode-' + mode)?.classList.add('selected');
  // Pi Remote collects a server URL right here on step 1.
  const remotePanel = document.getElementById('wiz-remote-panel');
  if (remotePanel) remotePanel.classList.toggle('d-none', mode !== 'pi_remote');
  // Pi Hosted: everything is local, so pre-fill the Grocy URL.
  if (mode === 'pi_hosted') {
    const urlField = document.getElementById('grocy_base_url');
    if (urlField && !urlField.value) urlField.value = PI_GROCY_DEFAULT;
  }
  // Pi Remote owns no data and has no local API, so the UI password and API
  // key do not apply: the remote server handles access control. Default auth
  // off, hide the API key, and offer the optional touchscreen PIN instead.
  const isRemote = mode === 'pi_remote';
  const apikeyRow = document.getElementById('wiz-apikey-row');
  if (apikeyRow) apikeyRow.classList.toggle('d-none', isRemote);
  const pinRow = document.getElementById('wiz-pin-row');
  if (pinRow) pinRow.classList.toggle('d-none', !isRemote);
  const pwBadge = document.getElementById('wiz-pw-badge');
  if (pwBadge) pwBadge.classList.toggle('d-none', isRemote);
  const secSub = document.getElementById('wiz-sec-sub');
  if (secSub) secSub.textContent = isRemote
    ? 'This device is a thin client; the server it controls handles login. A UI password here is optional.'
    : 'Set a password to protect your food inventory. Required before setup completes.';
  const authReq = document.getElementById('auth_required');
  if (authReq) {
    // Only force the default the first time we see this mode, so a user who
    // deliberately toggles it is not overridden on a progress-bar refresh.
    if (isRemote && authReq.dataset.remoteApplied !== '1') {
      authReq.checked = false;
      authReq.dataset.remoteApplied = '1';
      if (typeof toggleAuthRequired === 'function') toggleAuthRequired();
    } else if (!isRemote) {
      authReq.dataset.remoteApplied = '';
    }
  }
  _wizUpdateProgress();
}

function _wizUpdateProgress() {
  const seq = _wizStepSeq();
  for (let i = 1; i <= _WIZ_TOTAL; i++) {
    const dot = document.getElementById('dot-' + i);
    if (!dot) continue;
    // Hide the whole dot for steps this mode skips.
    const inSeq = seq.includes(i);
    dot.parentElement.classList.toggle('d-none', !inSeq);
    dot.classList.remove('active', 'done');
    if (!inSeq) continue;
    if (i < _wizStep) dot.classList.add('done');
    else if (i === _wizStep) dot.classList.add('active');
  }
  for (let i = 1; i < _WIZ_TOTAL; i++) {
    const con = document.getElementById('con-' + i + '-' + (i+1));
    if (!con) continue;
    // A connector is shown only when both steps it joins are in the sequence.
    const shown = seq.includes(i) && seq.includes(i + 1);
    con.classList.toggle('d-none', !shown);
    con.classList.toggle('done', shown && i < _wizStep);
  }
}

function _wizShowStep(n) {
  for (let i = 1; i <= _WIZ_TOTAL; i++) {
    const el = document.getElementById('wiz-step-' + i);
    if (!el) continue;
    el.classList.toggle('d-none', i !== n);
  }
  _wizStep = n;
  _wizUpdateProgress();
  window.scrollTo(0, 0);
}

// Persist the chosen mode (and remote URL) before leaving step 1, so a Pi's
// provisioner can read it and the server-side configured check matches.
async function _wizSaveMode() {
  try {
    await fetch('setup/mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        deployment_mode: _installMode,
        remote_server_url: val('remote_server_url') || '',
      }),
    });
  } catch (e) { /* non-fatal: the final Save also persists it */ }
}

// Satellite step 1: require the server URL + API key before advancing.
function _wizRemoteReady() {
  return Boolean(val('remote_server_url') && secretVal('upstream_api_key'));
}

function wizNext() {
  const seq = _wizStepSeq();
  const idx = seq.indexOf(_wizStep);
  if (idx < 0 || idx >= seq.length - 1) return;  // already on the last step

  // Per-step "on leave" hooks.
  if (_wizStep === 1) {
    if (_installMode === 'pi_remote' && !_wizRemoteReady()) {
      const r = document.getElementById('upstream-result');
      if (r) { r.className = 'test-result text-danger'; r.textContent = 'Enter the server URL and API key to continue.'; }
      return;
    }
    _wizSaveMode();
  }
  if (_wizStep === 4) {
    _updateGrocyOpenLink();
  }
  if (_wizStep === 5) wizUpdateAiKeyVisibility();

  const next = seq[idx + 1];
  if (next === 4) _wizApplyInstallMode();  // pre-fill/notes when entering Grocy
  if (next === 7) _wizBuildSummary();      // build summary before the Done step
  _wizShowStep(next);
}

function wizBack() {
  const seq = _wizStepSeq();
  const idx = seq.indexOf(_wizStep);
  if (idx <= 0) return;
  _wizShowStep(seq[idx - 1]);
}

async function testRemote() {
  const out = document.getElementById('remote-result');
  if (out) { out.className = 'test-result text-secondary'; out.textContent = 'Testing…'; }
  try {
    const r = await fetch('setup/test/remote', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({remote_server_url: val('remote_server_url') || ''}),
    });
    const d = await r.json();
    if (out) {
      out.className = 'test-result ' + (d.ok ? 'text-success' : 'text-danger');
      out.textContent = d.ok ? d.message : (d.error || 'Connection failed.');
    }
  } catch (e) {
    if (out) { out.className = 'test-result text-danger'; out.textContent = String(e); }
  }
}


function toggleStreamDeckOptions() {
  const checked = document.getElementById('has_streamdeck')?.checked;
  document.getElementById('wiz-streamdeck-opts')?.classList.toggle('d-none', !checked);
  document.getElementById('streamdeck-opts')?.classList.toggle('d-none', !checked);
}

function toggleDisplayOptions() {
  const checked = document.getElementById('wiz_has_display')?.checked;
  document.getElementById('wiz-display-opts')?.classList.toggle('d-none', !checked);
}

function syncScale() {
  const wizVal = document.getElementById('ui_scale_wiz')?.value;
  const settingsEl = document.getElementById('ui_scale');
  if (settingsEl && wizVal) settingsEl.value = wizVal;
}

function syncRotation() {
  const wizVal = document.getElementById('display_rotation_wiz')?.value;
  const settingsEl = document.getElementById('display_rotation');
  if (settingsEl && wizVal) settingsEl.value = wizVal;
}

function syncDisplayType() {
  const wizVal = document.getElementById('display_type_wiz')?.value;
  const settingsEl = document.getElementById('display_type');
  if (settingsEl && wizVal) settingsEl.value = wizVal;
  // A Waveshare HDMI HAT is a touch panel, so tick the touch flag to match.
  if (wizVal === 'waveshare_hdmi') {
    const touch = document.getElementById('display_touch');
    if (touch) touch.checked = true;
  }
}

function wizUpdateAiKeyVisibility() {
  const p = document.getElementById('vision_provider')?.value;
  const noneNote = document.getElementById('wiz-ai-none-note');
  const testRow = document.getElementById('wiz-ai-test-row');
  if (noneNote) noneNote.classList.toggle('d-none', p !== 'none');
  // Forager has its own sign-in button, so the key-oriented Test Connection
  // row applies to the manual providers only.
  if (testRow) testRow.classList.toggle('d-none', p === 'none' || p === 'cloud');
}

// Whether the Forager sign-in is done: linked before the page loaded, or
// signed in on this very step (cloudSignin sets the flag without a reload).
function _wizCloudLinked() {
  return (typeof WIZ_CLOUD_LINKED !== 'undefined' && WIZ_CLOUD_LINKED)
    || Boolean(window._cloudSignedIn);
}

// Step 4: respond to install mode choice made in Step 1
function _wizApplyInstallMode() {
  const applianceNote = document.getElementById('wiz-grocy-appliance-note');
  const urlField = document.getElementById('grocy_base_url');
  if (_installMode === 'pi_hosted') {
    if (urlField && !urlField.value) {
      urlField.value = PI_GROCY_DEFAULT;
    }
    if (applianceNote) applianceNote.classList.remove('d-none');
    _watchGrocyInstall();
  } else {
    if (applianceNote) applianceNote.classList.add('d-none');
  }
  // Update "Open Grocy" link
  _updateGrocyOpenLink();
}

// While the appliance's Grocy is still being installed/started (the first
// boot pulls and starts the whole stack), show the live install log instead
// of leaving the operator staring at nothing (FoodAssistant-n5ky). Polls the
// local Grocy probe; once it serves HTTP the log stops and a ready line shows.
let _grocyWatchActive = false;
async function _watchGrocyInstall() {
  if (_grocyWatchActive) return;
  if (document.documentElement.getAttribute('data-is-pi') !== '1') return;
  if (!document.querySelector('.install-log[data-log="grocy"]')) return;
  _grocyWatchActive = true;
  let serving = false;
  try {
    const s = await fetch('setup/grocy/local-status').then(x => x.json());
    if (!s || s.ok === false || s.serving) return;  // ready (or unknowable): no log needed
  } catch (e) { return; }
  setResult('grocy-install-result', true,
    'Grocy is still installing on this device. Live progress below; this step finishes on its own.');
  const stopLog = _startLogPolling('grocy', () => serving);
  try {
    // Poll every ~3s for up to ~10 minutes; a first boot pull can be slow.
    for (let i = 0; i < 200 && !serving; i++) {
      await _sleep(3000);
      let s;
      try {
        s = await fetch('setup/grocy/local-status').then(x => x.json());
      } catch (e) { continue; }
      if (s && s.ok !== false && s.serving) serving = true;
    }
    if (serving) {
      setResult('grocy-install-result', true, 'Grocy is up. Open it to create an API key, then paste it below.');
    } else {
      setResult('grocy-install-result', false, 'Grocy is taking longer than expected. Check the log above; it may still come up.');
    }
  } finally {
    serving = true;
    stopLog();
  }
}

// Build the Step 7 summary list
function _wizBuildSummary() {
  const el = document.getElementById('wiz-summary');
  if (!el) return;

  const authOn = document.getElementById('auth_required')?.checked ?? true;
  const hasPassword = HAS_AUTH_PASSWORD || !!secretVal('auth_password');
  const grocyUrl = val('grocy_base_url');
  const hasGrocyKey = WIZ_HAS_GROCY_KEY || !!secretVal('grocy_api_key');
  const provider = document.getElementById('vision_provider')?.value || 'none';
  const hasAiKey = provider === 'none' ? false
    : provider === 'ollama' ? true
    : provider === 'cloud' ? _wizCloudLinked()
    : (WIZ_HAS_AI_KEY || !!secretVal(provider + '_api_key'));
  const mealieUrl = val('mealie_base_url');
  const hasMealieKey = WIZ_HAS_MEALIE_KEY || !!secretVal('mealie_api_key');
  const mealieOk = mealieUrl && hasMealieKey;

  const items = [];

  // Security
  if (!authOn) {
    items.push({icon:'bi-shield-slash text-warning', label:'Authentication disabled', ok:true, detail:'An outer layer must handle access control.'});
  } else if (hasPassword) {
    items.push({icon:'bi-shield-check text-success', label:'Password set', ok:true, detail:''});
  } else {
    items.push({icon:'bi-shield-exclamation text-danger', label:'No password set', ok:false, step:2, detail:'Required to complete setup.'});
  }

  // Satellite: confirm the main server link; backend config is pulled from it.
  if (_installMode === 'pi_remote') {
    const remoteUrl = val('remote_server_url');
    if (remoteUrl && secretVal('upstream_api_key')) {
      items.push({icon:'bi-hdd-network text-success', label:'Pulls config from: ' + remoteUrl, ok:true, detail:'Grocy, Mealie, AI and defaults come from the main server.'});
    } else {
      items.push({icon:'bi-hdd-network text-danger', label:'Main server not set', ok:false, step:1, detail:'Server URL and API key are required.'});
    }
    el.innerHTML = items.map(item => `
      <div class="summary-item">
        <span class="summary-icon"><i class="bi ${item.icon}"></i></span>
        <span>${item.label}${item.detail ? '<span class="text-secondary ms-2 small">' + item.detail + '</span>' : ''}</span>
        ${!item.ok && item.step ? `<a href="#" class="summary-fix-link text-warning" onclick="event.preventDefault();_wizShowStep(${item.step})">Fix <i class="bi bi-arrow-right"></i></a>` : ''}
      </div>`).join('');
    const remoteErrs = items.filter(i => !i.ok && i.step);
    const eEl = document.getElementById('wiz-errors');
    const eList = document.getElementById('wiz-error-list');
    if (remoteErrs.length > 0 && eEl && eList) {
      eEl.classList.remove('d-none');
      eList.innerHTML = remoteErrs.map(e => `<li>${e.label}${e.detail ? ': ' + e.detail : ''}</li>`).join('');
    } else if (eEl) {
      eEl.classList.add('d-none');
    }
    return;
  }

  // Grocy
  if (grocyUrl && hasGrocyKey) {
    items.push({icon:'bi-fridge text-success', label:'Grocy: ' + grocyUrl, ok:true, detail:''});
  } else if (!grocyUrl) {
    items.push({icon:'bi-fridge text-danger', label:'Grocy URL not set', ok:false, step:4, detail:'Required.'});
  } else {
    items.push({icon:'bi-fridge text-warning', label:'Grocy URL set, no API key', ok:false, step:4, detail:'Add your Grocy API key.'});
  }

  // AI
  if (provider === 'none') {
    items.push({icon:'bi-magic text-secondary', label:'AI: not configured', ok:true, detail:'Photo import and enrichment will be unavailable.'});
  } else if (hasAiKey) {
    const providerLabel = {'cloud':'Forager','gemini':'Gemini','openai':'OpenAI','anthropic':'Anthropic','ollama':'Ollama'}[provider] || provider;
    items.push({icon:'bi-magic text-success', label:'AI: ' + providerLabel, ok:true, detail:''});
  } else if (provider === 'cloud') {
    items.push({icon:'bi-magic text-warning', label:'AI: Forager (not signed in)', ok:false, step:5, detail:'Sign in with your Forager account, or pick another provider or None.'});
  } else {
    const providerLabel = {'gemini':'Gemini','openai':'OpenAI','anthropic':'Anthropic'}[provider] || provider;
    items.push({icon:'bi-magic text-warning', label:'AI: ' + providerLabel + ' (no API key)', ok:false, step:5, detail:'Enter an API key or choose None.'});
  }

  // Mealie
  if (mealieOk) {
    items.push({icon:'bi-journal-richtext text-success', label:'Mealie: ' + mealieUrl, ok:true, detail:''});
  } else {
    items.push({icon:'bi-journal-richtext text-secondary', label:'Mealie: not configured', ok:true, detail:'Optional: add later from Settings.'});
  }

  // Hardware (pi_hosted only; pi_remote already returned above)
  if (_installMode === 'pi_hosted') {
    const hasDeck = document.getElementById('has_streamdeck')?.checked;
    const hasDisplay = document.getElementById('wiz_has_display')?.checked;
    if (hasDeck) {
      const kc = document.getElementById('streamdeck_key_count')?.value || '15';
      items.push({icon:'bi-hdd-stack text-info', label:`Stream Deck (${kc} keys)`, ok:true, detail:''});
    }
    if (hasDisplay) {
      const sz = document.getElementById('ui_scale_wiz')?.options[document.getElementById('ui_scale_wiz')?.selectedIndex]?.text || '';
      const rot = document.getElementById('display_rotation_wiz')?.value || '0';
      items.push({icon:'bi-display text-info', label:`Display: ${sz}${rot !== '0' ? ', rotated ' + rot + '°' : ''}`, ok:true, detail:''});
    }
  }

  el.innerHTML = items.map(item => `
    <div class="summary-item">
      <span class="summary-icon"><i class="bi ${item.icon}"></i></span>
      <span>${item.label}${item.detail ? '<span class="text-secondary ms-2 small">' + item.detail + '</span>' : ''}</span>
      ${!item.ok && item.step ? `<a href="#" class="summary-fix-link text-warning" onclick="event.preventDefault();_wizShowStep(${item.step})">Fix <i class="bi bi-arrow-right"></i></a>` : ''}
    </div>`).join('');

  // Errors
  const errors = items.filter(i => !i.ok && i.step);
  const errEl = document.getElementById('wiz-errors');
  const errList = document.getElementById('wiz-error-list');
  if (errors.length > 0 && errEl && errList) {
    errEl.classList.remove('d-none');
    errList.innerHTML = errors.map(e => `<li>${e.label}${e.detail ? ': ' + e.detail : ''}</li>`).join('');
  } else if (errEl) {
    errEl.classList.add('d-none');
  }
}

// Wizard save: collect all fields and POST to /setup/save, then redirect
async function wizSaveAll() {
  const btn = document.getElementById('wiz-finish-btn');
  const resultEl = document.getElementById('wiz-save-result');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving…';
  resultEl.innerHTML = '';

  const payload = buildPayload();

  // Pi Remote is a thin client: it has no local Grocy/AI/Mealie and does not
  // require a UI password (the remote server owns auth). The only thing it
  // needs is the address of the server it controls.
  if (_installMode === 'pi_remote') {
    if (!payload.remote_server_url || !secretVal('upstream_api_key')) {
      resultEl.innerHTML = '<div class="alert alert-danger py-2 small"><i class="bi bi-x-circle-fill me-1"></i>' +
        'Server URL and API key are required. <a href="#" onclick="event.preventDefault();_wizShowStep(1)">Go to Welcome</a></div>';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-rocket-takeoff me-2"></i>Start using Pantry Raider';
      return;
    }
  } else {
    // Enforce password requirement (full app only)
    if (payload.auth_required && !HAS_AUTH_PASSWORD
        && (!payload.auth_password || payload.auth_password === '__CLEAR__')) {
      resultEl.innerHTML = '<div class="alert alert-danger py-2 small"><i class="bi bi-x-circle-fill me-1"></i>' +
        'A password is required. <a href="#" onclick="event.preventDefault();_wizShowStep(2)">Go to Security</a></div>';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-rocket-takeoff me-2"></i>Start using Pantry Raider';
      return;
    }

    // Enforce Grocy URL (full app only)
    if (!payload.grocy_base_url) {
      resultEl.innerHTML = '<div class="alert alert-danger py-2 small"><i class="bi bi-x-circle-fill me-1"></i>' +
        'Grocy URL is required. <a href="#" onclick="event.preventDefault();_wizShowStep(4)">Go to Grocy</a></div>';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-rocket-takeoff me-2"></i>Start using Pantry Raider';
      return;
    }
  }

  try {
    const r = await fetch('setup/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.ok) {
      // Setup ran in this (possibly remote) browser. If a Pi kiosk display is
      // sitting on the wizard, it cannot navigate itself, so signal it to leave
      // the wizard and load the dashboard. Best effort: never block finishing.
      try {
        await fetch('setup/kiosk/navigate/request', { method: 'POST' });
      } catch (e) { /* kiosk hand-off is optional */ }
      // A satellite is a full local app (it pulled its config), so open its
      // own UI like any other mode.
      window.location.href = 'ui/';
    } else {
      throw new Error(d.detail || 'Unknown error');
    }
  } catch (e) {
    resultEl.innerHTML = `<div class="alert alert-danger py-2 small"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</div>`;
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-rocket-takeoff me-2"></i>Start using Pantry Raider';
  }
}

// Apply the saved/default deployment mode on load so the step-1 cards, the
// Pi Remote panel, and the progress bar reflect it before any click.
(function initWizardMode() {
  if (_installMode) selectInstallMode(_installMode);
  // Returning from a Google sign-in bounce (?cloud=done or ?cloud_error=…):
  // land back on the AI step instead of making the user re-walk the wizard.
  try {
    const q = new URLSearchParams(window.location.search);
    if ((q.has('cloud') || q.has('cloud_error'))
        && document.getElementById('wiz-step-5') && _wizStepSeq().includes(5)) {
      _wizShowStep(5);
    }
  } catch (e) { /* never block wizard init on this nicety */ }
})();
