// Per-section save (settings form). Posts only this section's fields; the
// server uses model_dump(exclude_unset=True), so untouched settings are kept.
// Drops keys whose element is absent (value undefined) so we never blank a
// field that does not render on this device.
async function savePane(fields, btn, resultId) {
  const el = document.getElementById(resultId);
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  if (el) el.innerHTML = '<span class="text-secondary">Saving...</span>';
  const payload = {};
  Object.keys(fields).forEach(k => { if (fields[k] !== undefined) payload[k] = fields[k]; });
  try {
    const r = await fetch('setup/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.detail || 'Unknown error');
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved.</span>';
    return true;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
    return false;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Helper: read a checkbox only when it exists, else undefined (so it is dropped).
function chk(id) { const e = document.getElementById(id); return e ? e.checked : undefined; }
// Helper: read a value only when the element exists, else undefined.
function optVal(id) { return document.getElementById(id) ? val(id) : undefined; }

function savePaneUpstream(btn) {
  return savePane({
    remote_server_url: optVal('remote_server_url'),
    upstream_api_key:  document.getElementById('upstream_api_key') ? secretVal('upstream_api_key') : undefined,
    extra_api_keys:    document.querySelector('.satellite-extra-keys') ? collectSatelliteKeys() : undefined,
  }, btn, 'upstream-save-result');
}

function savePaneGrocy(btn) {
  return savePane({
    grocy_base_url:   optVal('grocy_base_url'),
    grocy_api_key:    document.getElementById('grocy_api_key') ? secretVal('grocy_api_key') : undefined,
    grocy_public_url: optVal('grocy_public_url'),
  }, btn, 'grocy-save-result');
}

// Device hostname (link building) lives in the Devices pane; it has its own
// save so the card posts only its own field.
function saveDeviceHostname(btn) {
  return savePane({ device_hostname: optVal('device_hostname') }, btn, 'device-hostname-result');
}

// Phone QR code card (Connections pane).
function saveQrSettings(btn) {
  return savePane({
    qr_url_mode: optVal('qr_url_mode'),
    qr_public_url: optVal('qr_public_url'),
  }, btn, 'qr-save-result');
}

function savePaneAi(btn) {
  return savePane({
    vision_provider:   optVal('vision_provider'),
    gemini_api_key:    document.getElementById('gemini_api_key') ? secretVal('gemini_api_key') : undefined,
    gemini_model:      document.getElementById('gemini_model') ? (val('gemini_model') || 'gemini-2.5-flash') : undefined,
    ollama_base_url:   optVal('ollama_base_url'),
    ollama_model:      document.getElementById('ollama_model') ? (val('ollama_model') || 'llava:7b') : undefined,
    openai_api_key:    document.getElementById('openai_api_key') ? secretVal('openai_api_key') : undefined,
    openai_model:      document.getElementById('openai_model') ? (val('openai_model') || 'gpt-4o-mini') : undefined,
    anthropic_api_key: document.getElementById('anthropic_api_key') ? secretVal('anthropic_api_key') : undefined,
    anthropic_model:   document.getElementById('anthropic_model') ? (val('anthropic_model') || 'claude-opus-4-8') : undefined,
    ai_extra_keys:     document.querySelector('.extra-keys') ? collectExtraKeys() : undefined,
    barcode_enrichment: optVal('barcode_enrichment'),
    barcode_llm_fallback: chk('barcode_llm_fallback'),
    barcode_autocheck_shopping: chk('barcode_autocheck_shopping'),
    enrich_provider:   optVal('enrich_provider'),
    enrich_model:      optVal('enrich_model'),
    llm_expiry_enabled: chk('llm_expiry_enabled'),
    ai_token_budget:   document.getElementById('ai_token_budget') ? num('ai_token_budget', 0) : undefined,
  }, btn, 'ai-save-result');
}

// AI token usage + budget (Pantry Raider).
async function _loadAiUsage() {
  const el = document.getElementById('ai-usage-display');
  if (!el) return;
  try {
    const d = await fetch('setup/ai-usage').then(r => r.json());
    if (!d.ok) { el.textContent = 'Usage unavailable.'; return; }
    const fmt = n => (n || 0).toLocaleString();
    // Approximate spend, priced with the selected model's list prices. Null
    // means the model is not in the price table: show tokens only, no guess.
    const money = v => (v == null) ? '' :
      ' <span class="text-secondary">(~$' + (v > 0 && v < 0.01 ? '0.01' : v.toFixed(2)) + ')</span>';
    const by = Object.entries(d.by_provider || {}).filter(([, v]) => v)
      .map(([k, v]) => k + ' ' + fmt(v)).join(', ');
    let html = '<div><i class="bi bi-graph-up me-1"></i>This month (' + d.month_key + '): <strong>' + fmt(d.month) + '</strong> tokens' + money(d.cost_month);
    if (d.budget) {
      const pct = Math.min(100, Math.round(d.month / d.budget * 100));
      const bar = d.over_budget ? 'bg-danger' : (pct >= 80 ? 'bg-warning' : 'bg-success');
      html += ' of ' + fmt(d.budget) + ' budget' + (d.over_budget ? ' <span class="text-danger">(reached)</span>' : '')
        + '</div><div class="progress mt-1" style="max-width:420px;height:8px"><div class="progress-bar ' + bar + '" style="width:' + pct + '%"></div></div>';
    } else { html += '</div>'; }
    html += '<div class="mt-1">All time: ' + fmt(d.total) + ' tokens' + money(d.cost_total) + (by ? ' (' + by + ')' : '') + '</div>';
    if (d.cost_total != null && (d.total || d.month)) {
      html += '<div class="mt-1 text-secondary" style="font-size:.85em">Dollar figures are rough estimates: input and output tokens are counted together, so they are priced at a blended rate from the list prices for ' + (d.cost_model || 'your selected model').replace(/[<>&"]/g, '') + ', which may have changed since this version shipped. Your provider\'s bill is the real number.</div>';
    }
    el.innerHTML = html;
  } catch (e) { el.textContent = 'Usage unavailable.'; }
}
function saveAiBudget(btn) {
  return savePane({ ai_token_budget: num('ai_token_budget', 0) }, btn, 'ai-budget-result')
    .then(() => _loadAiUsage());
}
async function resetAiUsage(btn) {
  if (!confirm('Reset the recorded AI token usage to zero?')) return;
  const el = document.getElementById('ai-budget-result');
  try {
    await fetch('setup/ai-usage/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Usage reset.</span>';
    _loadAiUsage();
  } catch (e) { if (el) el.innerHTML = '<span class="text-danger">' + e + '</span>'; }
}

// Forager sign-in (FoodAssistant-t6ab). One call does everything: the email
// and password go to the cloud (only there, never stored here), the install
// gets its own credential back, and a fresh install is switched to Forager
// for scanning automatically. The settings card reloads into its linked
// state; the wizard stays on its step and marks AI as done.
async function cloudSignin(btn, prefix) {
  prefix = prefix || '';
  const el = document.getElementById(prefix + 'cloud-signin-result');
  const esc = s => String(s || '').replace(/[<>&"]/g, '');
  const say = (ok, msg) => {
    if (el) el.innerHTML = '<span class="' + (ok ? 'text-success' : 'text-danger') + '">' +
      '<i class="bi ' + (ok ? 'bi-check-circle' : 'bi-x-circle-fill') + ' me-1"></i>' + msg + '</span>';
  };
  const email = (document.getElementById(prefix + 'cloud_email')?.value || '').trim();
  const password = document.getElementById(prefix + 'cloud_password')?.value || '';
  // Normalize the kitchen name to the subdomain-safe form the portal expects,
  // trimming any leading/trailing dash, so a friendly name never trips sign-in.
  const nameRaw = (document.getElementById(prefix + 'cloud_kitchen_name')?.value || '').trim();
  const name = (typeof _slugKitchenName === 'function' ? _slugKitchenName(nameRaw) : nameRaw)
    .replace(/^-+|-+$/g, '');
  // The 2FA code, sent only after the cloud asks for it (the field is hidden
  // until then). A recovery code is accepted here too.
  const totp = (document.getElementById(prefix + 'cloud_totp')?.value || '').trim();
  const totpWrap = document.getElementById(prefix + 'cloud_totp_wrap');
  if (!email || !password) { say(false, 'Enter your Forager email and password.'); return; }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Signing in…';
  try {
    const r = await fetch('setup/cloud/signin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, device_name: name, totp }),
    });
    const d = await r.json();
    // The account has two-factor sign-in on: reveal the code field, keep the
    // password so a resubmit works, and let them enter the code.
    if (!d.ok && d.totp_prompt) {
      if (totpWrap) totpWrap.classList.remove('d-none');
      const codeEl = document.getElementById(prefix + 'cloud_totp');
      if (codeEl) codeEl.focus();
      say(false, esc(d.error || 'Enter the code from your authenticator app.'));
      btn.disabled = false;
      btn.innerHTML = orig;
      return;
    }
    if (!d.ok) throw new Error(d.error || 'Sign-in failed. Try again.');
    const pw = document.getElementById(prefix + 'cloud_password');
    if (pw) pw.value = '';
    const codeEl = document.getElementById(prefix + 'cloud_totp');
    if (codeEl) codeEl.value = '';
    if (totpWrap) totpWrap.classList.add('d-none');
    if (prefix) {
      // Wizard: stay on the step so nothing typed elsewhere is lost.
      window._cloudSignedIn = true;
      say(true, 'Signed in as ' + esc(d.account_email) + '. Scanning is ready.');
      btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Signed in';
    } else {
      say(true, 'Signed in. Reloading…');
      setTimeout(() => location.reload(), 600);
    }
  } catch (e) {
    say(false, esc(e.message));
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// One-click switch offered when a sign-in preserved an already-working
// provider: point scanning and enrichment at Forager and re-render.
async function cloudUseForager(btn) {
  btn.disabled = true;
  await savePane({ vision_provider: 'cloud', enrich_provider: 'cloud' }, btn, 'cloud-signin-result');
  setTimeout(() => location.reload(), 600);
}

// "Continue with Google" is shown only when the cloud is reachable and says
// Google sign-in is on; anything else means no button and no error.
async function _initCloudMeta() {
  const btns = ['cloud-google-btn', 'wiz_cloud-google-btn']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (!btns.length) return;
  try {
    const d = await fetch('setup/cloud/meta').then(r => r.json());
    if (d && d.oauth_google && d.google_start_url) {
      window._cloudGoogleStartUrl = d.google_start_url;
      btns.forEach(b => b.classList.remove('d-none'));
    }
  } catch (e) { /* no button, no error */ }
}

// Send the browser to Forager's Google sign-in. The return address is built
// from this page's own origin, so it works on the LAN address today and a
// public address later; the flow hint brings the user back to the right
// place (settings card or wizard step).
function cloudGoogleStart(prefix) {
  if (!window._cloudGoogleStartUrl) return;
  prefix = prefix || '';
  const name = (document.getElementById(prefix + 'cloud_kitchen_name')?.value || '').trim();
  const ret = new URL('setup/cloud/oauth-return', document.baseURI);
  ret.searchParams.set('flow', prefix ? 'wizard' : 'settings');
  const u = new URL(window._cloudGoogleStartUrl);
  u.searchParams.set('flow', 'app');
  if (name) u.searchParams.set('device_name', name);
  u.searchParams.set('return_url', ret.toString());
  window.location.href = u.toString();
}

// After a Google sign-in bounce, a failure comes back as ?cloud_error=…;
// show it in whichever sign-in card is on the page.
function _showCloudReturnNotice() {
  let err = '';
  try { err = new URLSearchParams(window.location.search).get('cloud_error') || ''; } catch (e) { return; }
  if (!err) return;
  const el = document.getElementById('cloud-signin-result')
    || document.getElementById('wiz_cloud-signin-result');
  if (el) el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>' +
    err.replace(/[<>&"]/g, '') + '</span>';
}

// Forager pairing (docs/design/cloud-platform.md). Link redeems a
// pairing code for an instance token stored server-side; the page reloads so
// the card re-renders in its linked state. All failures land in the result
// line; an unreachable cloud never breaks the pane.
async function cloudLink(btn) {
  const el = document.getElementById('cloud-link-result');
  const code = (document.getElementById('cloud_pairing_code')?.value || '').trim();
  if (!code) { if (el) el.innerHTML = '<span class="text-danger">Enter the pairing code from the cloud portal.</span>'; return; }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Linking…';
  try {
    const r = await fetch('setup/cloud/link', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'The pairing code was not accepted.');
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Linked. Reloading…</span>';
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function cloudUnlink(btn) {
  if (!confirm('Switch this kitchen to self-hosted? Your inventory, recipes, settings, and device password all stay. Photo, receipt, and barcode scanning through Forager and reaching your kitchen from away will turn off until you connect again.')) return;
  const el = document.getElementById('cloud-link-result');
  btn.disabled = true;
  try {
    await fetch('setup/cloud/unlink', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Switched to self-hosted. Reloading…</span>';
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e.message}</span>`;
    btn.disabled = false;
  }
}

// Fill the cloud status card and the cloud quota line in the usage card.
async function _loadCloudStatus() {
  const statusEl = document.getElementById('cloud-status');
  const usageEl = document.getElementById('cloud-usage-display');
  if (!statusEl && !usageEl) return;
  const fail = msg => {
    if (statusEl) statusEl.innerHTML = '<span class="text-warning"><i class="bi bi-cloud-slash me-1"></i>' + msg + '</span>';
    if (usageEl) usageEl.textContent = 'Cloud quota unavailable right now.';
  };
  try {
    const d = await fetch('setup/cloud/status').then(r => r.json());
    if (!d.linked) { if (statusEl) statusEl.textContent = 'Not linked.'; if (usageEl) usageEl.textContent = ''; return; }
    if (!d.reachable || !d.valid) { fail(d.error || 'The cloud link could not be checked.'); return; }
    const ent = d.entitlement || {};
    const fmt = n => (n || 0).toLocaleString();
    const esc = s => String(s || '').replace(/[<>&"]/g, '');
    let html = '<i class="bi bi-cloud-check me-1"></i>' + (d.account_email
      ? 'Signed in as <strong>' + esc(d.account_email) + '</strong> (this device: ' + esc(d.name || 'unnamed') + ')'
      : 'Connected as <strong>' + esc(d.name || 'this device') + '</strong>');
    // Plan-aware: a paid plan reads as active, a trial shows days left, an
    // expired trial nudges to upgrade. A working plan is never a warning.
    const plan = String(ent.plan || '').replace(/[<>&"]/g, '');
    if (ent.active) {
      html += ' · ' + (plan ? plan + ' plan, ' : '') + 'subscription active';
    } else if (plan === 'trial') {
      const d = ent.trial_days_left;
      html += ' · free trial' + (d != null ? ', ' + d + ' day' + (d === 1 ? '' : 's') + ' left' : '');
    } else if (ent.entitled) {
      html += ' · ' + (plan ? plan + ' plan' : 'plan active');
    } else {
      html += ' · <span class="text-warning">trial ended, subscribe to keep cloud scanning</span>';
    }
    statusEl && (statusEl.innerHTML = html);
    if (usageEl) {
      if (ent.quota) {
        const pct = Math.min(100, Math.round((ent.used || 0) / ent.quota * 100));
        const bar = (ent.used || 0) >= ent.quota ? 'bg-danger' : (pct >= 80 ? 'bg-warning' : 'bg-info');
        usageEl.innerHTML = '<div><i class="bi bi-cloud me-1"></i>Forager (' + (ent.month || '') + '): <strong>' +
          fmt(ent.used) + '</strong> of ' + fmt(ent.quota) + ' tokens' +
          '</div><div class="progress mt-1" style="max-width:420px;height:8px"><div class="progress-bar ' + bar + '" style="width:' + pct + '%"></div></div>';
      } else {
        usageEl.innerHTML = '<i class="bi bi-cloud me-1"></i>Forager: ' +
          (ent.entitled ? 'no monthly quota set for this plan.'
                        : 'your trial has ended. Subscribe to keep cloud scanning.');
      }
    }
  } catch (e) { fail('Forager could not be reached.'); }
}

// Forager remote access (FoodAssistant-uczr). Fill the card, then let the
// user turn the WireGuard hub tunnel on or off. It works on a Pi appliance
// (the host bridge owns the interface) and on a server with WireGuard support
// (the app runs the tunnel in its own container); a server without that
// support gets an honest error from the enable route. Every function is a
// no-op when its section is absent.
async function _loadTunnelStatus() {
  const section = document.getElementById('tunnel-section');
  if (!section) return;
  const statusEl = document.getElementById('tunnel-status');
  const controls = document.getElementById('tunnel-controls');
  const onControls = document.getElementById('tunnel-on-controls');
  const enableBtn = document.getElementById('tunnel-enable-btn');
  const chooser = document.getElementById('tunnel-address-chooser');
  try {
    const d = await fetch('setup/tunnel/status').then(r => r.json());
    controls && controls.classList.remove('d-none');
    if (d.enabled) {
      enableBtn && enableBtn.classList.add('d-none');
      chooser && chooser.classList.add('d-none');
      onControls && onControls.classList.remove('d-none');
      const link = document.getElementById('tunnel-url-link');
      if (link && d.public_url) { link.href = d.public_url; link.textContent = d.public_url; }
      if (statusEl) {
        const hs = (d.last_handshake_seconds != null && d.last_handshake_seconds < 180)
          ? 'connected' : (d.up ? 'waiting for the device to connect' : 'starting up');
        statusEl.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Remote access is on (' + hs + ').</span>';
      }
    } else {
      enableBtn && enableBtn.classList.remove('d-none');
      chooser && chooser.classList.remove('d-none');
      onControls && onControls.classList.add('d-none');
      if (statusEl) statusEl.textContent = 'Remote access is off.';
    }
  } catch (e) {
    controls && controls.classList.remove('d-none');
    if (statusEl) statusEl.textContent = 'Remote access status is unavailable right now.';
  }
}

// Turn on Forager (WireGuard hub) remote access. Shared by the settings pane
// and the first-time wizard: pass a result element id and a follow-up refresh
// so the wizard can show its own status without duplicating the settings ids.
// The server enforces the gates (linked, login password set, host can host a
// tunnel) and returns the message shown here.
async function tunnelEnable(btn, resultId, after) {
  const el = document.getElementById(resultId || 'tunnel-result');
  const refresh = (typeof after === 'function') ? after : _loadTunnelStatus;
  // The chosen web address, if the Web address field is present (settings pane).
  // Sanitized to the same subdomain-safe form the cloud accepts; blank keeps
  // the device-name default.
  const subEl = document.getElementById('tunnel_subdomain');
  const subdomain = subEl
    ? (typeof _slugKitchenName === 'function' ? _slugKitchenName(subEl.value) : subEl.value.trim())
        .replace(/^-+|-+$/g, '')
    : '';
  btn.disabled = true;
  if (el) el.innerHTML = '<span class="text-secondary">Turning on remote access…</span>';
  try {
    const d = await fetch('setup/tunnel/enable', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subdomain }),
    }).then(r => r.json());
    if (!d.ok) {
      let msg = d.error || 'Remote access could not be turned on.';
      // The chosen address was taken: offer the free suggestion right here.
      if (d.subdomain_taken && d.suggestion) {
        msg += ' Try <strong>' + String(d.suggestion).replace(/[<>&"]/g, '') + '</strong>.';
        if (subEl) { subEl.value = d.suggestion; tunnelSubdomainInput(subEl); }
      }
      if (el) el.innerHTML = '<span class="text-danger">' + msg + '</span>';
      btn.disabled = false;
      return;
    }
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Remote access is on.</span>';
    // Keep tunnel_mode (the single source of truth) in step with the WireGuard tunnel.
    try { await fetch('setup/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tunnel_mode: 'forager' }) }); } catch (_) { }
    await refresh();
    btn.disabled = false;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e.message}</span>`;
    btn.disabled = false;
  }
}

async function tunnelDisable(btn) {
  if (!confirm('Turn off remote access? Your kitchen will only be reachable on your home network again.')) return;
  const el = document.getElementById('tunnel-result');
  btn.disabled = true;
  try {
    const d = await fetch('setup/tunnel/disable', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.json());
    if (el) el.innerHTML = d.ok
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Remote access is off.</span>'
      : '<span class="text-danger">' + (d.error || 'Could not turn off remote access.') + '</span>';
    if (d.ok) {
      // Clear tunnel_mode so the single source of truth matches the tunnel being off.
      try { await fetch('setup/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tunnel_mode: '' }) }); } catch (_) { }
    }
    await _loadTunnelStatus();
    btn.disabled = false;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e.message}</span>`;
    btn.disabled = false;
  }
}

function tunnelCopyUrl(btn) {
  const link = document.getElementById('tunnel-url-link');
  const url = link && (link.textContent || link.href);
  if (!url) return;
  // copyText (helpers.js) falls back to execCommand on plain-HTTP LAN and
  // kiosk browsers, where navigator.clipboard is unavailable.
  const t = btn.textContent;
  copyText(url, null).then(ok => {
    btn.textContent = ok ? 'copied' : 'copy failed';
    setTimeout(() => { btn.textContent = t; }, 1200);
  });
}

// Live availability check for the chosen Forager web address. Sanitizes as the
// user types, then (debounced) asks the cloud, through the app proxy, whether
// the address is free and shows a free suggestion when it is taken. A blank
// field just restores the default hint.
let _tunnelSubTimer = null;
function tunnelSubdomainInput(el) {
  if (!el) return;
  // Keep the field to the subdomain-safe characters the address allows.
  const cleaned = (typeof _slugKitchenName === 'function' ? _slugKitchenName(el.value) : el.value);
  if (cleaned !== el.value) el.value = cleaned;
  const hint = document.getElementById('tunnel-subdomain-hint');
  const name = cleaned.replace(/^-+|-+$/g, '');
  if (_tunnelSubTimer) clearTimeout(_tunnelSubTimer);
  if (!name) {
    if (hint) { hint.className = 'form-text'; hint.textContent = 'Choose the address people use to reach your kitchen. Leave it blank to use your device name.'; }
    return;
  }
  if (hint) { hint.className = 'form-text text-secondary'; hint.textContent = 'Checking that address…'; }
  _tunnelSubTimer = setTimeout(async () => {
    try {
      const d = await fetch('setup/tunnel/subdomain-available?name=' + encodeURIComponent(name)).then(r => r.json());
      if (!hint) return;
      if (!d.ok) { hint.className = 'form-text text-secondary'; hint.textContent = d.error || 'That address could not be checked right now.'; return; }
      const apex = d.apex ? ('.' + d.apex) : '';
      if (d.available) {
        hint.className = 'form-text text-success';
        hint.innerHTML = '<i class="bi bi-check-circle me-1"></i>' + String(d.sanitized || name).replace(/[<>&"]/g, '') + apex.replace(/[<>&"]/g, '') + ' is available.';
      } else {
        hint.className = 'form-text text-danger';
        const sug = String(d.suggestion || '').replace(/[<>&"]/g, '');
        hint.innerHTML = '<i class="bi bi-x-circle me-1"></i>That address is taken.' + (sug ? ' Try <strong>' + sug + '</strong>.' : '');
      }
    } catch (e) {
      if (hint) { hint.className = 'form-text text-secondary'; hint.textContent = 'That address could not be checked right now.'; }
    }
  }, 400);
}

// Collect the checked kitchen appliances. Absent container (e.g. on a satellite
// where Preferences may differ) returns undefined so the field is not posted and
// the stored selection is left alone; otherwise an explicit (possibly empty) list.
function collectAppliances() {
  const box = document.getElementById('kitchen-appliances');
  if (!box) return undefined;
  return Array.from(box.querySelectorAll('.appliance-chk'))
    .filter(c => c.checked).map(c => c.value);
}

function applianceSelectAll(state) {
  document.querySelectorAll('#kitchen-appliances .appliance-chk')
    .forEach(c => { c.checked = state; });
  syncStandMixerAttachments();
}

// Stand mixer attachments are only relevant when a stand mixer is owned
// (FoodAssistant-rjdr). Hide that group unless the stand_mixer box is checked,
// on load and whenever it changes. The Shop side already filters server-side;
// this keeps the checklist UI in step.
function syncStandMixerAttachments() {
  const owns = document.getElementById('appliance_stand_mixer');
  const group = document.querySelector('#kitchen-appliances .appliance-group[data-group="attachment"]');
  if (!group) return;
  group.style.display = (owns && owns.checked) ? '' : 'none';
}

function savePaneRecipes(btn) {
  return savePane({
    mealie_base_url:   optVal('mealie_base_url'),
    mealie_api_key:    document.getElementById('mealie_api_key') ? secretVal('mealie_api_key') : undefined,
    mealie_public_url: optVal('mealie_public_url'),
    recipe_source:     optVal('recipe_source'),
    // Where the shopping list lives ("" automatic | grocy | mealie).
    shopping_backend:  optVal('shopping_backend'),
    themealdb_api_key: document.getElementById('themealdb_api_key') ? secretVal('themealdb_api_key') : undefined,
    spoonacular_api_key: document.getElementById('spoonacular_api_key') ? secretVal('spoonacular_api_key') : undefined,
    forager_recipes_enabled: chk('forager_recipes_enabled'),
  }, btn, 'recipes-save-result');
}

// Copy the Mealie recipe library into Pantry Raider's own store
// (FoodAssistant-g0fd). Same endpoint the Recipes page button uses; safe to
// run again (already-copied recipes are skipped) and Mealie is never changed.
async function migrateRecipesFromMealie(btn) {
  const el = document.getElementById('recipes-migrate-result');
  if (!confirm('Copy all your Mealie recipes into Pantry Raider and keep them here from now on? Mealie itself is not changed.')) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Copying…';
  if (el) el.innerHTML = '<span class="text-secondary">Copying your recipes…</span>';
  try {
    const r = await fetch('recipes/migrate-from-mealie', {method: 'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Unknown error');
    if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${d.message} Reload this page to see the change.</span>`;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// The community-recipes toggle saves itself the moment it flips (its own panel
// has no Save button, and on a satellite the Save Recipes button is absent). It
// is per-device, so it posts even where the other recipe fields are read-only.
function saveForagerRecipes(el) {
  return savePane({ forager_recipes_enabled: chk('forager_recipes_enabled') },
                  el, 'recipes-save-result');
}

// Add a set of community recipes to the local library in one action
// (FoodAssistant-l2hk). Confirms first, shows a working state, then reports how
// many were added versus skipped. The server skips recipes already in the
// library, so this is safe to run more than once. A missing Mealie library or a
// disconnected Forager account comes back as a friendly message here.
async function bundleCommunityRecipes(btn) {
  const el = document.getElementById('forager-bundle-result');
  if (!confirm('Add up to 30 community recipes to your library? Ones you already have are skipped.')) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Adding…';
  if (el) el.innerHTML = '<span class="text-secondary">Adding community recipes to your library...</span>';
  try {
    const r = await fetch('mealie/recipes/bundle-community', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Unknown error');
    if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${d.message}</span>`;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Recipe suggestion tuning + the kitchen appliances checklist: its own
// Recipe suggestions pane (FoodAssistant-ysj1), saved by its own button. This
// scope is deliberately disjoint from savePaneRecipes so saving tastes never
// re-posts the Mealie connection fields (base url, api key, recipe source).
function savePaneRecipePrefs(btn) {
  return savePane({
    staple_items:      optVal('staple_items'),
    cook_ai_context:   optVal('cook_ai_context'),
    kitchen_appliances: collectAppliances(),
    perishable_days:   document.getElementById('perishable_days') ? num('perishable_days', 14) : undefined,
    expiring_soon_days: document.getElementById('expiring_soon_days') ? num('expiring_soon_days', 5) : undefined,
    suggest_per_tier:  document.getElementById('suggest_per_tier') ? num('suggest_per_tier', 8) : undefined,
  }, btn, 'recipe-prefs-save-result');
}

// Navigation Tabs card save (Appearance pane): just the tab editor. Quiet mode
// saves with the nav-bar card under Screen & Sleep, the QR address under
// Connections, and theme changes persist through applyTheme()/saveCustomTheme().
function savePaneNavigation(btn) {
  return savePane({
    ...navPayload(),
  }, btn, 'interface-save-result');
}

// Save the custom-theme builder as a NAMED theme (FoodAssistant-nw49). Posts the
// name + base + five swatches; the server stores it in custom_themes (keyed by a
// slug of the name), makes it the active theme, and we reload to apply it.
async function saveCustomTheme(btn) {
  const out = document.getElementById('custom-theme-result');
  const name = (document.getElementById('custom_theme_name')?.value || '').trim();
  if (!name) {
    if (out) out.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Give the theme a name.</span>';
    document.getElementById('custom_theme_name')?.focus();
    return;
  }
  const payload = {
    name,
    base:    optVal('custom_theme_base') || 'dark',
    primary: optVal('custom_theme_primary'),
    accent:  optVal('custom_theme_accent'),
    bg:      optVal('custom_theme_bg'),
    surface: optVal('custom_theme_surface'),
    text:    optVal('custom_theme_text'),
  };
  const orig = btn.innerHTML; btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  if (out) out.innerHTML = '<span class="text-secondary">Saving...</span>';
  try {
    const r = await fetch('setup/custom-theme', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Save failed');
    location.reload();
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    if (out) out.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  }
}

// Delete the currently-active saved custom theme and fall back to the default
// theme. Only rendered when a "custom:<id>" theme is active.
async function deleteCustomTheme(btn) {
  const out = document.getElementById('custom-theme-result');
  if (!confirm('Delete this saved custom theme?')) return;
  const orig = btn.innerHTML; btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Deleting…';
  try {
    const r = await fetch('setup/custom-theme/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Delete failed');
    location.reload();
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    if (out) out.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  }
}

// Background image (FoodAssistant-e2t6). Upload posts the file to the dedicated
// endpoint (which stores it and sets background_image_url to the serve route);
// Save persists the URL/opacity through /save; Remove clears both. Each reloads
// so the new background applies on this page too.
async function uploadBackground(btn) {
  const out = document.getElementById('background-result');
  const f = document.getElementById('background_file');
  if (!f || !f.files || !f.files.length) {
    if (out) out.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Choose an image first.</span>';
    return;
  }
  const fd = new FormData();
  fd.append('file', f.files[0]);
  const orig = btn.innerHTML; btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Uploading…';
  try {
    // Save the opacity first so an upload keeps the slider value the user set.
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({background_opacity: num('background_opacity', 40)}),
    });
    const r = await fetch('setup/background', {method: 'POST', body: fd});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Upload failed');
    location.reload();
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    if (out) out.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  }
}

function saveBackground(btn) {
  return savePane({
    background_image_url: optVal('background_image_url'),
    background_opacity:   num('background_opacity', 40),
  }, btn, 'background-result').then(() => { setTimeout(() => location.reload(), 400); });
}

async function clearBackground(btn) {
  const out = document.getElementById('background-result');
  const orig = btn.innerHTML; btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Removing…';
  try {
    const r = await fetch('setup/background/clear', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Failed');
    location.reload();
  } catch (e) {
    btn.disabled = false; btn.innerHTML = orig;
    if (out) out.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  }
}

// Reset the nav editor to the built-in default order/grouping/visibility
// (FoodAssistant-oret). Rebuilds navState from the pristine defaults that the
// server passes as TABS_DEFAULT, then re-renders. Save persists it.
function resetNavEditor(btn) {
  if (!confirm('Reset the navigation tabs to their defaults? Custom tabs and headings you added will be removed.')) return;
  navState = (window.TABS_DEFAULT || TABS).map(t => ({
    key: t.key, label: t.label, icon: t.icon,
    hidden: !!t.hidden, available: t.available !== false,
    custom: !!t.custom, heading: !!t.heading,
    parent: t.parent || '', url: t.href || '',
  }));
  renderNavEditor();
  const out = document.getElementById('interface-save-result');
  if (out) out.innerHTML = '<span class="text-secondary"><i class="bi bi-info-circle me-1"></i>Reset to defaults. Click Save Navigation to keep it.</span>';
}

function savePaneSecurity(btn) {
  const auth_required = document.getElementById('auth_required')?.checked ?? true;
  const auth_password = document.getElementById('auth_password') ? secretVal('auth_password') : undefined;
  const current_password = document.getElementById('current_password')?.value || undefined;
  // A real new password must match its confirmation before we post it, so a
  // typo cannot silently become the login password and lock the owner out.
  // Blank (keep the current one) and the clear button (__CLEAR__) are exempt:
  // there is no new value to confirm. The confirm field is a client-side check
  // only, never posted (see the savePane payload below).
  const pwEl = document.getElementById('auth_password');
  const confirmEl = document.getElementById('auth_password_confirm');
  if (pwEl && confirmEl && pwEl.value
      && auth_password !== '__CLEAR__' && confirmEl.value !== pwEl.value) {
    const el = document.getElementById('security-save-result');
    if (el) el.innerHTML =
      '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>The new passwords do not match.</span>';
    confirmEl.focus();
    return;
  }
  // Secure by default: block save if auth is required but no password is set or stored.
  if (auth_required && !HAS_AUTH_PASSWORD
      && (!auth_password || auth_password === '__CLEAR__')) {
    const el = document.getElementById('security-save-result');
    if (el) el.innerHTML =
      '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Set a UI password, ' +
      'or turn off "Require authentication" if an outer layer handles it.</span>';
    document.querySelector('[data-bs-target="#pane-security"]')?.click();
    document.getElementById('auth_password')?.focus();
    return;
  }
  return savePane({
    auth_required: auth_required,
    auth_password: auth_password,
    current_password: current_password,
    viewer_password: document.getElementById('viewer_password') ? secretVal('viewer_password') : undefined,
    api_key:       document.getElementById('api_key') ? secretVal('api_key') : undefined,
    kiosk_pin:     document.getElementById('kiosk_pin') ? secretVal('kiosk_pin') : undefined,
    kiosk_readonly_when_locked: chk('kiosk_readonly_when_locked'),
    // LAN device pairing toggle (FoodAssistant-4box); undefined on a satellite,
    // where the control does not render.
    local_device_pairing_enabled: chk('local_device_pairing_enabled'),
    // The Add satellite key rows live in this pane, so its Save must post them or
    // a newly added satellite key is collected but never persisted (the satellite
    // then gets a 401 because the server never stored its key).
    extra_api_keys: document.querySelector('.satellite-extra-keys') ? collectSatelliteKeys() : undefined,
  }, btn, 'security-save-result');
}

// The Remote Access section (Forager pane) has no standalone Save. Cloudflare
// writes tunnel_mode + tunnel_token through Connect/Disconnect (tunnel/start,
// tunnel/stop, which run the container); Forager writes tunnel_mode alongside
// the WireGuard turn on/off (setup/tunnel/enable, setup/tunnel/disable).

function savePaneData(btn) {
  return savePane({
    rclone_remote:         optVal('rclone_remote'),
    rclone_schedule_hours: document.getElementById('rclone_schedule_hours') ? num('rclone_schedule_hours', 0) : undefined,
    usb_backup_interval_hours: document.getElementById('usb_backup_interval_hours') ? num('usb_backup_interval_hours', 0) : undefined,
  }, btn, 'data-save-result');
}

// USB flash-drive backup: drive status plus a manual run.
async function loadUsbStatus() {
  const el = document.getElementById('usb-status');
  if (!el) return;
  try {
    const r = await fetch('admin/backup/usb/status');
    const d = await r.json();
    if (!d.detected) {
      el.innerHTML = '<span class="text-secondary"><i class="bi bi-usb-symbol me-1"></i>No USB drive detected. '
        + 'Plug in a formatted drive and it is picked up automatically.</span>';
      return;
    }
    const free = (d.free_bytes / 1073741824).toFixed(1);
    let msg = 'USB drive at ' + d.mountpoint + ' (' + free + ' GB free)';
    if (d.last_backup) {
      // Honor the 12/24-hour setting; 'auto' keeps the browser locale reading.
      const bd = new Date(d.last_backup_time * 1000);
      const cf = document.documentElement.getAttribute('data-clock-format');
      const stamp = (cf === '12' || cf === '24')
        ? bd.toLocaleString(undefined, { hour12: cf === '12' })
        : bd.toLocaleString();
      msg += '. Last backup: ' + stamp + '.';
    } else msg += '. No backups on it yet.';
    el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' + msg + '</span>';
  } catch (e) {
    el.innerHTML = '<span class="text-secondary">Drive status is unavailable right now.</span>';
  }
}
document.addEventListener('DOMContentLoaded', loadUsbStatus);

// Back up to Forager (FoodAssistant-kzjz). The panel is rendered only when the
// install is linked, then this shows it only when the account is Premium (the
// cloud is the real gate). Fail-soft: not Premium or an unreachable cloud just
// leaves the panel hidden, and the local backup below always works.
async function loadForagerBackup() {
  const panel = document.getElementById('forager-backup-panel');
  if (!panel) return;
  let d;
  try {
    d = await fetch('setup/cloud/backup/status').then(r => r.json());
  } catch (e) { panel.classList.add('d-none'); return; }
  if (!d.linked || !d.premium) { panel.classList.add('d-none'); return; }
  panel.classList.remove('d-none');
  const statusEl = document.getElementById('forager-backup-status');
  const sel = document.getElementById('forager-restore-select');
  const controls = document.getElementById('forager-restore-controls');
  const empty = document.getElementById('forager-restore-empty');
  const backups = d.backups || [];
  const fmtSize = n => (n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB'
    : n >= 1024 ? (n / 1024).toFixed(0) + ' KB' : (n || 0) + ' B');
  if (statusEl) {
    statusEl.innerHTML = backups.length
      ? '<i class="bi bi-cloud-check me-1"></i>Last backup to Forager: <strong>'
        + String(backups[0].created_at || '').slice(0, 16).replace('T', ' ') + '</strong> ('
        + backups.length + ' stored)'
      : '<i class="bi bi-cloud me-1"></i>No Forager backups yet.';
  }
  if (sel && controls && empty) {
    if (backups.length) {
      sel.innerHTML = backups.map(b => '<option value="' + b.id + '">'
        + String(b.created_at || '').slice(0, 16).replace('T', ' ')
        + ' (' + fmtSize(b.size_bytes) + ')</option>').join('');
      controls.classList.remove('d-none');
      empty.classList.add('d-none');
    } else {
      controls.classList.add('d-none');
      empty.classList.remove('d-none');
    }
  }
}
document.addEventListener('DOMContentLoaded', loadForagerBackup);

async function foragerBackupNow(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Backing up…';
  try {
    const inc = document.getElementById('backup_include_secrets')?.checked;
    const d = await postJson('setup/cloud/backup/upload' + (inc ? '?include_secrets=true' : ''), {});
    setResult('forager-backup-result', d.ok, d.ok ? (d.message || 'Backed up to Forager.') : d.error);
    if (d.ok) loadForagerBackup();
  } catch (e) {
    setResult('forager-backup-result', false, 'Backup failed.');
  } finally {
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function foragerRestore(btn) {
  const sel = document.getElementById('forager-restore-select');
  const pwField = document.getElementById('forager_restore_password');
  if (!confirm('Restore this kitchen from Forager? This replaces the current '
      + 'settings and database. The current data is copied aside first, but you '
      + 'should reload the page after.')) {
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restoring…';
  try {
    const body = { restore_password: pwField?.value || '' };
    const id = sel?.value;
    if (id) body.backup_id = parseInt(id, 10);
    const d = await postJson('setup/cloud/backup/restore', body);
    if (!d.ok) throw new Error(d.error || 'Restore failed.');
    let msg = 'Restored ' + d.restored_files + ' file(s).';
    if (d.secrets_preserved) msg += ' Kept ' + d.secrets_preserved + ' existing secret(s).';
    msg += ' Reload to see the restored settings.';
    setResult('forager-restore-result', true, msg);
  } catch (e) {
    setResult('forager-restore-result', false, e.message);
  } finally {
    if (pwField) pwField.value = '';
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function usbBackupNow(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Backing up…';
  try {
    const d = await postJson('admin/backup/usb', {});
    if (!d.ok) throw new Error(d.detail || d.error || 'Backup failed');
    setResult('usb-backup-result', true, 'Backup saved to ' + d.file);
    loadUsbStatus();
  } catch (e) {
    setResult('usb-backup-result', false, e.message);
  }
  btn.disabled = false;
  btn.innerHTML = orig;
}

function savePaneHardware(btn) {
  return savePane({
    scanner_type:           optVal('scanner_type'),
    barcode_global_capture: chk('barcode_global_capture'),
  }, btn, 'hardware-save-result');
}

// Build the full settings payload from form fields, shared by wizard and settings form.
function buildPayload() {
  const providerEl = document.getElementById('vision_provider');
  const providerVal = providerEl ? providerEl.value : 'gemini';
  // "none" persists as-is: the app runs without AI and falls back to the
  // no-op provider (see is_configured / ai_configured server-side).

  return {
    vision_provider: providerVal,
    gemini_api_key:  secretVal('gemini_api_key'),
    gemini_model:    val('gemini_model') || 'gemini-2.5-flash',
    ollama_base_url: val('ollama_base_url'),
    ollama_model:    val('ollama_model') || 'llava:7b',
    openai_api_key:  secretVal('openai_api_key'),
    openai_model:    val('openai_model') || 'gpt-4o-mini',
    anthropic_api_key: secretVal('anthropic_api_key'),
    anthropic_model: val('anthropic_model') || 'claude-opus-4-8',
    // Only sent from the settings form (the wizard has no extra-key rows), so
    // first-time setup leaves stored extras untouched.
    ai_extra_keys: document.querySelector('.extra-keys') ? collectExtraKeys() : undefined,
    extra_api_keys: document.querySelector('.satellite-extra-keys') ? collectSatelliteKeys() : undefined,
    scanner_type:       (document.getElementById('scanner_type') || document.getElementById('wiz-scanner_type'))?.value || '',
    barcode_global_capture: document.getElementById('barcode_global_capture') ? document.getElementById('barcode_global_capture').checked : true,
    barcode_enrichment: val('barcode_enrichment') || 'llm',
    barcode_llm_fallback: document.getElementById('barcode_llm_fallback')?.checked || false,
    barcode_autocheck_shopping: document.getElementById('barcode_autocheck_shopping')?.checked || false,
    enrich_provider: val('enrich_provider'),
    enrich_model:    val('enrich_model'),
    grocy_base_url:  val('grocy_base_url'),
    grocy_api_key:   secretVal('grocy_api_key'),
    grocy_public_url: val('grocy_public_url'),
    device_hostname: val('device_hostname'),
    mealie_base_url: val('mealie_base_url'),
    mealie_api_key:  secretVal('mealie_api_key'),
    mealie_public_url: val('mealie_public_url'),
    recipe_source:   (document.getElementById('recipe_source_wiz') || document.getElementById('recipe_source'))?.value || 'themealdb',
    themealdb_api_key: secretVal('themealdb_api_key'),
    spoonacular_api_key: secretVal('spoonacular_api_key_wiz') || secretVal('spoonacular_api_key'),
    staple_items:    val('staple_items'),
    cook_ai_context: val('cook_ai_context'),
    perishable_days: num('perishable_days', 14),
    expiring_soon_days: num('expiring_soon_days', 5),
    suggest_per_tier: num('suggest_per_tier', 8),
    ...navPayload(),
    ui_theme:        document.getElementById('ui_theme')?.value || 'dark',
    ui_scale:        document.getElementById('ui_scale_wiz')?.value || document.getElementById('ui_scale')?.value || 'normal',
    display_rotation: parseInt(document.getElementById('display_rotation_wiz')?.value ?? document.getElementById('display_rotation')?.value ?? '0', 10),
    display_type:    document.getElementById('display_type_wiz')?.value || document.getElementById('display_type')?.value || 'generic',
    has_streamdeck:  document.getElementById('has_streamdeck')?.checked || false,
    streamdeck_key_count: parseInt(document.getElementById('streamdeck_key_count')?.value || '0', 10),
    streamdeck_idle_timeout: parseInt(document.getElementById('streamdeck_idle_timeout')?.value || '0', 10),
    display_touch:   document.getElementById('display_touch')?.checked || false,
    display_idle_timeout: parseInt(document.getElementById('display_idle_timeout')?.value || '0', 10),
    screensaver_minutes: parseInt(document.getElementById('screensaver_minutes')?.value || '0', 10),
    screensaver_speed: document.getElementById('screensaver_speed')?.value || 'normal',
    screensaver_mode: document.getElementById('screensaver_mode')?.value || 'bounce',
    screensaver_all_clients: document.getElementById('screensaver_all_clients')?.checked || false,
    osk_enabled:     document.getElementById('osk_enabled')?.checked ?? true,
    wake_on_motion:  document.getElementById('wake_on_motion')?.value || 'auto',
    auth_required:   document.getElementById('auth_required')?.checked ?? true,
    auth_password:   secretVal('auth_password'),
    api_key:         secretVal('api_key'),
    rclone_remote:   val('rclone_remote'),
    rclone_schedule_hours: num('rclone_schedule_hours', 0),
    usb_backup_interval_hours: document.getElementById('usb_backup_interval_hours') ? num('usb_backup_interval_hours', 0) : undefined,
    // _installMode is defined only in the first-time wizard; on the settings
    // form it is undefined and these are simply omitted (kept as-is server-side).
    deployment_mode: (typeof _installMode !== 'undefined' && _installMode) ? _installMode : undefined,
    remote_server_url: val('remote_server_url') || undefined,
    upstream_api_key: secretVal('upstream_api_key'),
    kiosk_pin:       secretVal('kiosk_pin'),
    kiosk_readonly_when_locked: document.getElementById('kiosk_readonly_when_locked')?.checked ?? false,
  };
}

// TOTP 2FA
let _totpSecret = '';

async function setupTOTP() {
  const section = document.getElementById('totp-setup');
  section.classList.remove('d-none');
  document.getElementById('totp-result').innerHTML = '';
  document.getElementById('totp-qr').src = '';
  document.getElementById('totp-secret-display').textContent = 'Generating…';
  try {
    const d = await postJson('setup/totp/generate', {});
    _totpSecret = d.secret;
    document.getElementById('totp-qr').src = d.qr;
    document.getElementById('totp-secret-display').textContent = 'Manual key: ' + d.secret;
  } catch(e) {
    document.getElementById('totp-secret-display').textContent = 'Error: ' + e.message;
  }
}

// Recovery codes are shown exactly once, so render them prominently and make
// them easy to copy. There is no way to see them again after leaving the page.
function _recoveryCodesHtml(codes) {
  const list = (codes || []).map(c => '<code>' + c + '</code>').join(' ');
  return '<div class="alert alert-warning py-2 mt-2 small">'
    + '<div class="fw-semibold mb-1"><i class="bi bi-key me-1"></i>Save these recovery codes now</div>'
    + '<div>Each works once if you lose your authenticator. They are not shown again.</div>'
    + '<div class="mt-2 font-monospace" style="line-height:1.9">' + list + '</div></div>';
}

async function verifyTOTP() {
  const code = document.getElementById('totp-code').value.trim();
  if (!_totpSecret || !code) return;
  const d = await postJson('setup/totp/verify', {secret: _totpSecret, code});
  const el = document.getElementById('totp-result');
  if (d.ok) {
    el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' + d.message + '</span>'
      + _recoveryCodesHtml(d.recovery_codes);
    // Leave the codes on screen; reload only when the user acts again.
    document.getElementById('totp-code').value = '';
  } else {
    el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>' + d.error + '</span>';
  }
}

async function disableTOTP() {
  const credential = (document.getElementById('totp-disable-code') || {}).value || '';
  const el = document.getElementById('totp-manage-result');
  const d = await postJson('setup/totp/disable', {credential: credential.trim()});
  if (d.ok) { location.reload(); return; }
  el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>' + (d.error || 'Failed') + '</span>';
}

async function regenRecoveryCodes() {
  const credential = (document.getElementById('totp-disable-code') || {}).value || '';
  const el = document.getElementById('totp-manage-result');
  const d = await postJson('setup/totp/recovery', {credential: credential.trim()});
  if (d.ok) {
    el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' + d.message + '</span>'
      + _recoveryCodesHtml(d.recovery_codes);
  } else {
    el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>' + (d.error || 'Failed') + '</span>';
  }
}

// Rclone remote
async function testRcloneRemote(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
  await fetch('setup/save', {method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({rclone_remote: val('rclone_remote'), rclone_schedule_hours: num('rclone_schedule_hours',0)})});
  const d = await postJson('admin/backup/test-remote', {});
  setResult('rclone-result', d.ok, d.ok ? d.message : d.error);
  btn.disabled = false;
  btn.innerHTML = orig;
}

async function pushToRemote(btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Pushing…';
  try {
    const inc = document.getElementById('backup_include_secrets')?.checked;
    const d = await postJson('admin/backup/remote' + (inc ? '?include_secrets=true' : ''), {});
    if (!d.ok) throw new Error(d.detail || 'Unknown error');
    btn.innerHTML = '<i class="bi bi-check-circle me-1"></i>Done';
    btn.className = btn.className.replace('btn-outline-secondary','btn-outline-success');
    setTimeout(() => { btn.innerHTML = orig; btn.className = btn.className.replace('btn-outline-success','btn-outline-secondary'); btn.disabled = false; }, 3000);
  } catch(e) {
    alert('Push failed: ' + e.message);
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function restoreBackup(btn) {
  const input = document.getElementById('restore-file');
  const result = document.getElementById('restore-result');
  const f = input?.files?.[0];
  if (!f) {
    if (result) result.innerHTML = '<span class="text-warning">Choose a backup zip first.</span>';
    return;
  }
  if (!confirm('Restore from "' + f.name + '"? This replaces the current settings and '
      + 'database. The current data is copied aside first, but you should reload the page after.')) {
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restoring…';
  if (result) result.innerHTML = '';
  try {
    const pwField = document.getElementById('restore_password');
    const fd = new FormData();
    fd.append('file', f);
    fd.append('restore_password', pwField?.value || '');
    const r = await fetch('admin/restore', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.detail || 'Restore failed.');
    let msg = 'Restored ' + d.restored_files + ' file(s).';
    if (d.secrets_preserved) msg += ' Kept ' + d.secrets_preserved + ' existing secret(s).';
    msg += ' Reload to see the restored settings.';
    if (result) result.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>'
      + msg + '</span>';
  } catch (e) {
    if (result) result.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle me-1"></i>'
      + e.message + '</span>';
  } finally {
    if (pwField) pwField.value = '';
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

async function fullRestore(btn) {
  const input = document.getElementById('full-restore-source');
  const result = document.getElementById('full-restore-result');
  const source = (input?.value || '').trim();
  if (!source) {
    if (result) result.innerHTML = '<span class="text-warning">Enter a device path or rclone:remote path first.</span>';
    return;
  }
  if (!confirm('Full-stack restore from:\n\n' + source + '\n\nThis STOPS the whole container stack '
      + '(Grocy and Mealie included) and REPLACES all data. The current data is moved aside first '
      + '(nothing is deleted) and the stack restarts. Continue?')) {
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restoring…';
  if (result) result.innerHTML = '<span class="text-secondary">Restoring the full stack; this can take a few minutes…</span>';
  try {
    const d = await postJson('setup/restore', { source: source });
    if (!d.ok) throw new Error(d.error || 'Restore failed.');
    let msg = 'Restored ' + ((d.restored_dirs || []).join(', ') || 'snapshot') + '.';
    if (d.snapshot) msg += ' Previous data kept in .pre-restore-' + d.snapshot + '.';
    msg += d.restarted ? ' Stack restarted.' : ' WARNING: the stack may not have restarted: check the device.';
    if (result) result.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>'
      + msg + '</span>';
  } catch (e) {
    if (result) result.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle me-1"></i>'
      + e.message + '</span>';
  } finally {
    btn.innerHTML = orig;
    btn.disabled = false;
  }
}

// Remote Access / Tunnel
let _tunnelPollTimer = null;

// One remote-access section, two modes. The radio picks the view: Cloudflare
// shows its token + Connect/Disconnect; Forager shows the WireGuard turn
// on/off controls (no token, it uses the account link). The action buttons in
// each mode persist tunnel_mode, so the radio is only a selector here.
function tunnelModeChanged() {
  const mode = document.querySelector('input[name="tunnel_mode"]:checked')?.value || '';
  const cf = document.getElementById('tunnel-cf-controls');
  const forager = document.getElementById('tunnel-section');
  if (cf) cf.style.display = (mode === 'cloudflare') ? '' : 'none';
  if (forager) forager.style.display = (mode === 'forager') ? '' : 'none';
  // The Forager sub-controls fill from live status; refresh when it is shown.
  if (mode === 'forager' && typeof _loadTunnelStatus === 'function') _loadTunnelStatus();
}

function _setTunnelBadge(state, url) {
  const badge = document.getElementById('tunnel-status-badge');
  const urlEl = document.getElementById('tunnel-url-display');
  if (!badge) return;
  if (state === 'connected') {
    badge.className = 'badge bg-success';
    badge.textContent = 'Connected';
  } else if (state === 'connecting') {
    badge.className = 'badge bg-warning text-dark';
    badge.textContent = 'Connecting…';
  } else {
    badge.className = 'badge bg-secondary';
    badge.textContent = 'Not connected';
  }
  if (urlEl) {
    if (url) {
      urlEl.innerHTML = `<a href="${url}" target="_blank" class="text-info">${url}</a>`;
    } else {
      urlEl.textContent = '';
    }
  }
}

async function tunnelConnect() {
  const mode = document.querySelector('input[name="tunnel_mode"]:checked')?.value || '';
  if (!mode) { setResult('tunnel-result', false, 'Select a mode first.'); return; }
  const token = document.getElementById('tunnel_token')?.value.trim() || '';
  const btn = document.getElementById('tunnel-connect-btn');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Connecting…';
  _setTunnelBadge('connecting', '');
  setResult('tunnel-result', true, 'Starting tunnel…');
  _stopTunnelPoll();

  try {
    const d = await postJson('tunnel/start', {mode, token});
    if (d.ok) {
      setResult('tunnel-result', true, 'Tunnel started, waiting for URL…');
      _startTunnelPoll();
    } else {
      _setTunnelBadge('disconnected', '');
      setResult('tunnel-result', false, d.error || 'Failed to start tunnel.');
    }
  } catch(e) {
    _setTunnelBadge('disconnected', '');
    setResult('tunnel-result', false, 'Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

async function tunnelDisconnect() {
  _stopTunnelPoll();
  const btn = document.getElementById('tunnel-disconnect-btn');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Stopping…';

  try {
    const d = await postJson('tunnel/stop', {});
    _setTunnelBadge('disconnected', '');
    setResult('tunnel-result', d.ok, d.ok ? 'Tunnel stopped.' : (d.error || 'Failed to stop tunnel.'));
  } catch(e) {
    setResult('tunnel-result', false, 'Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

function _startTunnelPoll() {
  _tunnelPollTimer = setInterval(async () => {
    try {
      const r = await fetch('tunnel/status');
      const d = await r.json();
      if (d.running && d.url) {
        _stopTunnelPoll();
        _setTunnelBadge('connected', d.url);
        setResult('tunnel-result', true, 'Tunnel active.');
      } else if (d.running) {
        _setTunnelBadge('connecting', '');
      } else {
        _stopTunnelPoll();
        _setTunnelBadge('disconnected', '');
        setResult('tunnel-result', false, 'Tunnel stopped unexpectedly.');
      }
    } catch(_) { /* ignore transient errors */ }
  }, 5000);
}

function _stopTunnelPoll() {
  if (_tunnelPollTimer) { clearInterval(_tunnelPollTimer); _tunnelPollTimer = null; }
}

async function _initTunnelStatus() {
  try {
    const r = await fetch('tunnel/status');
    const d = await r.json();
    if (d.running && d.url) {
      _setTunnelBadge('connected', d.url);
    } else if (d.running) {
      _setTunnelBadge('connecting', '');
      _startTunnelPoll();
    }
  } catch(_) { /* docker may not be available */ }
}


// --- Printing pane (FoodAssistant-fb8x) ----------------------------------

// Save the printing settings. Only this pane's fields are posted, so the master
// toggle, chosen queues, and label size persist without touching anything else.
function savePanePrinting(btn) {
  const enabled = chk('printing_enabled');
  const payload = {
    printing_enabled:  enabled,
    label_width_in:    parseFloat(val('label_width_in')) || 2.0,
    label_height_in:   parseFloat(val('label_height_in')) || 1.0,
    label_dpi:         parseInt(val('label_dpi'), 10) || 203,
    label_shape:       val('label_shape') || 'rectangle',
    label_show_logo:   chk('label_show_logo'),
  };
  // Square and round stock is 1:1, so persist an equal height regardless of the
  // (disabled) height field.
  if (payload.label_shape === 'square' || payload.label_shape === 'round') {
    payload.label_height_in = payload.label_width_in;
  }
  if (document.getElementById('document_page_size')) {
    payload.document_page_size  = val('document_page_size') || 'auto';
    payload.document_color_mode = val('document_color_mode') || 'color';
    payload.document_duplex     = val('document_duplex') || 'one-sided';
  }
  // On a server / Pi Hosted the pane picks the FLEET default queues, which every
  // device inherits (FoodAssistant-7u7z). On a Pi Remote it picks this device's
  // own LOCAL override, which wins over the inherited default. Only the selectors
  // that this mode renders are posted, so the save never clobbers the other.
  if (document.getElementById('fleet_label_printer_queue')) {
    payload.fleet_label_printer_queue    = optVal('fleet_label_printer_queue');
    payload.fleet_document_printer_queue = optVal('fleet_document_printer_queue');
  }
  if (document.getElementById('label_printer_queue')) {
    payload.label_printer_queue    = optVal('label_printer_queue');
    payload.document_printer_queue = optVal('document_printer_queue');
  }
  return savePane(payload, btn, 'printing-save-result')
    .then(ok => { if (ok) updatePrintingPill(enabled); });
}

// Flip the Printing header pill to match the saved master switch, so the pane
// reflects the toggle without a page reload. The pill is server-rendered on
// load, so only the just-saved state needs updating here.
function updatePrintingPill(enabled) {
  const pill = document.getElementById('printing-pill');
  if (!pill) return;
  pill.textContent = enabled ? 'On' : 'Off';
  pill.classList.toggle('set-pill-good', !!enabled);
  pill.classList.toggle('set-pill-neutral', !enabled);
}

// Install the print stack on this device (FoodAssistant-gyri). Shown only when
// no print system is set up yet. On a Pi this runs the on-device installer
// (CUPS + Bluetooth + drivers); on a server it returns the steps to start the
// printing service. On success we refresh the printer list so found queues show.
async function installPrintStack(btn) {
  const el = document.getElementById('print-install-result');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Setting up printing…';
  if (el) el.innerHTML = '<span class="text-secondary">This can take a couple of minutes on a Pi.</span>';
  try {
    const r = await fetch('printing/install', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    const msg = (d.message || d.detail || r.statusText || '').replace(/\n/g, '<br>');
    if (r.ok && d.ok) {
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${msg}</span>`;
      // Reveal the discovered printers now that the stack is up.
      loadPrintQueues(true);
    } else {
      if (el) el.innerHTML = `<span class="text-warning"><i class="bi bi-info-circle me-1"></i>${msg}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Could not set up printing: ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Fill the printer dropdowns from the device's CUPS queues. Keeps the currently
// saved value selected even if it is not in the discovered list (a printer that
// is momentarily offline should not vanish from the setting).
let _printQueuesLoaded = false;
async function loadPrintQueues(force) {
  if (_printQueuesLoaded && !force) return;
  _printQueuesLoaded = true;
  const result = document.getElementById('print-queues-result');
  if (result && force) result.innerHTML = '<span class="text-secondary">Looking for printers…</span>';
  try {
    const r = await fetch('printing/queues');
    const d = await r.json();
    const queues = d.queues || [];
    // Fill whichever selectors this mode rendered: the fleet-default pair on a
    // server / Pi Hosted, and the local-override pair on a Pi Remote. Missing ids
    // are skipped, so one list of fleet-wide queues (local plus any discovered
    // shared printer) feeds them all.
    ['label_printer_queue', 'document_printer_queue',
     'fleet_label_printer_queue', 'fleet_document_printer_queue'].forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      const current = sel.value;
      const names = queues.map(q => q.name);
      sel.innerHTML = '<option value="">(none chosen)</option>' +
        queues.map(q => `<option value="${q.name}">${q.name}${q.is_default ? ' (default)' : ''}</option>`).join('');
      // Keep a saved-but-offline printer selectable.
      if (current && !names.includes(current)) {
        sel.insertAdjacentHTML('beforeend', `<option value="${current}">${current}</option>`);
      }
      sel.value = current;
    });
    if (result) {
      result.innerHTML = queues.length
        ? `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Found ${queues.length} printer(s).</span>`
        : (d.available
            ? '<span class="text-secondary">No printers found on this device yet.</span>'
            : '<span class="text-secondary">No print system is set up on this device yet.</span>');
    }
  } catch (e) {
    if (result) result.innerHTML = `<span class="text-danger">Could not read printers: ${e.message}</span>`;
  }
}

// --- Discover + add printers (FoodAssistant-r9a4) ------------------------

// Look for printers on the network that this device could add. Renders each
// found printer with an Add button; adding one creates the CUPS queue and then
// refreshes the dropdowns so it can be picked as a label or document printer.
async function findPrinters(btn) {
  const el = document.getElementById('printer-discover-result');
  const list = document.getElementById('printer-discover-list');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Looking…';
  if (el) el.innerHTML = '';
  if (list) list.innerHTML = '';
  try {
    const r = await fetch('printing/discover');
    const d = await r.json().catch(() => ({}));
    const printers = d.printers || [];
    if (!r.ok || d.ok === false) {
      if (el) el.innerHTML = `<span class="text-warning"><i class="bi bi-info-circle me-1"></i>${(d.message || d.detail || r.statusText || 'Could not look for printers.')}</span>`;
      return;
    }
    if (!printers.length) {
      // A Ready Bluetooth bridge only shows the printer once it is powered on
      // and in range (FoodAssistant-h2j6); nudge toward that instead of
      // leaving an empty result unexplained.
      const btHint = _btPrintReady
        ? ' If your Bluetooth label printer isn’t listed, make sure it is turned on nearby, then try again.'
        : '';
      if (el) el.innerHTML = `<span class="text-secondary">No new printers found on your network.${btHint}</span>`;
      return;
    }
    if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Found ${printers.length} printer(s) you can add.</span>`;
    if (list) list.innerHTML = printers.map((p, i) => {
      const advanced = p.kind === 'socket' || p.kind === 'other';
      const label = p.info || p.name;
      const badge = advanced
        ? ' <span class="badge text-bg-secondary">advanced</span>'
        : '';
      // Each printer gets an editable name (pre-filled with a valid default) so
      // you can call it what you like before adding; the name becomes its queue,
      // so it stays letters/digits/dashes/underscores with no spaces.
      return `<div class="border rounded p-2 mb-2" style="max-width:640px">
        <div><strong>${label}</strong>${badge}</div>
        <div class="small text-secondary mb-2">${p.uri}</div>
        <div class="d-flex align-items-center gap-2 flex-wrap">
          <label class="small text-secondary mb-0" for="disc-name-${i}">Name</label>
          <input type="text" class="form-control form-control-sm" id="disc-name-${i}"
                 value="${_gEsc(p.name || '')}" spellcheck="false" autocomplete="off"
                 style="max-width:240px" aria-label="Printer name"
                 oninput="this.value=this.value.replace(/[^A-Za-z0-9_-]/g,'_')">
          <button type="button" class="btn btn-outline-info btn-sm" onclick='addDiscoveredPrinter(this, ${JSON.stringify(p)}, ${i})'>
            <i class="bi bi-plus-lg me-1"></i>Add
          </button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Could not look for printers: ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Add a printer found by the search, under the name typed in its row (falling
// back to the suggested one), the device URI, and the driver the discovery
// classified (everywhere for driverless, raw for a socket printer).
async function addDiscoveredPrinter(btn, printer, i) {
  const input = document.getElementById('disc-name-' + i);
  const name = ((input && input.value) || printer.name || '').trim();
  const el = document.getElementById('printer-discover-result');
  if (!name) {
    if (el) el.innerHTML = '<span class="text-warning"><i class="bi bi-info-circle me-1"></i>Give the printer a name first.</span>';
    if (input) input.focus();
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Adding…';
  try {
    await _postAddPrinter({
      name: name,
      connection: printer.uri,
      model: printer.driver || 'everywhere',
    }, el);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Show the port field only for a raw socket, whose default port is 9100.
function onAddPrinterTypeChange() {
  const type = val('add-printer-type');
  const row = document.getElementById('add-printer-port-row');
  // Socket and Zebra both connect over a raw socket, so both show the port.
  if (row) row.style.display = (type === 'socket' || type === 'zebra') ? '' : 'none';
}

// Build a device URI from the host/type/port fields and add the printer.
async function addPrinterByAddress(btn) {
  const el = document.getElementById('add-printer-result');
  const name = val('add-printer-name').trim();
  const host = val('add-printer-host').trim();
  const type = val('add-printer-type');
  if (!name) { if (el) el.innerHTML = '<span class="text-danger">Give the printer a name first.</span>'; return; }
  if (!host) { if (el) el.innerHTML = '<span class="text-danger">Enter the printer host name or IP address.</span>'; return; }
  let connection, model;
  if (type === 'socket' || type === 'zebra') {
    const port = parseInt(val('add-printer-port'), 10) || 9100;
    connection = `socket://${host}:${port}`;
    // A Zebra label printer speaks ZPL: add it with the bundled ZPL driver so
    // rendered labels rasterize. A plain raw socket takes no driver.
    model = (type === 'zebra') ? 'zebra-zpl' : 'raw';
  } else {
    // Driverless and Network (IPP) both add over IPP Everywhere.
    connection = `ipp://${host}/ipp/print`;
    model = 'everywhere';
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Adding…';
  try {
    await _postAddPrinter({ name, connection, model }, el);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Shared POST /printing/add + on success refresh the queue dropdowns so the new
// printer can be selected right away.
async function _postAddPrinter(payload, resultEl) {
  try {
    const r = await fetch('printing/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      if (resultEl) resultEl.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${d.message || 'Printer added.'}</span>`;
      loadPrintQueues(true);
    } else {
      const msg = (d.message || d.detail || r.statusText || 'Could not add the printer.');
      if (resultEl) resultEl.innerHTML = `<span class="text-warning"><i class="bi bi-info-circle me-1"></i>${msg}</span>`;
    }
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Could not add the printer: ${e.message}</span>`;
  }
}

// Live label preview: POST the current size to the preview endpoint and show the
// returned PNG. Debounced so dragging a number field does not flood the server.
let _labelPreviewTimer = null;
function refreshLabelPreview() {
  clearTimeout(_labelPreviewTimer);
  _labelPreviewTimer = setTimeout(_doLabelPreview, 250);
}
async function _doLabelPreview() {
  const img = document.getElementById('label-preview-img');
  if (!img) return;
  try {
    const r = await fetch('printing/label/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: 'Sample label',
        added: '2026-07-01', best_by: '2026-07-15', best_by_source: 'default',
        width_in: parseFloat(val('label_width_in')) || 2.0,
        height_in: parseFloat(val('label_height_in')) || 1.0,
        dpi: parseInt(val('label_dpi'), 10) || 203,
        show_logo: chk('label_show_logo'),
      }),
    });
    if (!r.ok) return;
    const blob = await r.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url = URL.createObjectURL(blob);
    img.dataset.url = url;
    img.src = url;
  } catch (_) { /* preview is best-effort */ }
}

let _decorativePreviewTimer = null;
function refreshDecorativePreview() {
  clearTimeout(_decorativePreviewTimer);
  _decorativePreviewTimer = setTimeout(_doDecorativePreview, 250);
}
async function _doDecorativePreview() {
  const img = document.getElementById('decorative-preview-img');
  if (!img) return;
  try {
    const r = await fetch('printing/decorative/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: val('decorative-text') || 'Sample',
        width_in: parseFloat(val('label_width_in')) || 2.0,
        height_in: parseFloat(val('label_height_in')) || 1.0,
        dpi: parseInt(val('label_dpi'), 10) || 203,
        icon: val('decorative-icon') || '',
        outline: !!chk('decorative-outline'),
      }),
    });
    if (!r.ok) return;
    const blob = await r.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url = URL.createObjectURL(blob);
    img.dataset.url = url;
    img.src = url;
  } catch (_) { /* preview is best-effort */ }
}

// Populate the decorative-label symbol picker from the same curated icon set
// the field designer uses (FoodAssistant-nxr8). Safe to call any time; a
// failed fetch just leaves the "No symbol" option in place.
async function loadDecorativeIcons() {
  const sel = document.getElementById('decorative-icon');
  if (!sel) return;
  try {
    const r = await fetch('printing/decorative/icons');
    const d = await r.json();
    const icons = (d && d.icons) || [];
    sel.innerHTML = '<option value="">No symbol</option>' +
      icons.map(i => `<option value="${i.key}">${i.glyph} ${i.key}</option>`).join('');
  } catch (_) { /* leave the placeholder option */ }
}

async function printDecorative(btn) {
  const el = document.getElementById('decorative-print-result');
  const text = val('decorative-text');
  if (!text) { if (el) el.innerHTML = '<span class="text-danger">Enter the text for the label first.</span>'; return; }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Printing…';
  if (el) el.innerHTML = '';
  try {
    const r = await fetch('printing/decorative', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text,
        width_in: parseFloat(val('label_width_in')) || 2.0,
        height_in: parseFloat(val('label_height_in')) || 1.0,
        dpi: parseInt(val('label_dpi'), 10) || 203,
        icon: val('decorative-icon') || '',
        outline: !!chk('decorative-outline'),
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.error || r.statusText);
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Sent to the label printer.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}


// ---------------------------------------------------------------------------
// Bluetooth label printer (Supvan T50M family) setup (FoodAssistant-h2j6). The
// printer connects over Bluetooth, not the network, so a small on-device
// bridge (the Supvan CUPS printer-app) is needed to turn it into an ordinary
// IPP-Everywhere network printer; once that is set up, Find printers above
// discovers it exactly like any other network printer. Appliance only
// (pi_hosted or pi_remote); the panel renders guidance with no button on a
// plain server. The first setup builds the bridge on the device (several
// minutes), so this polls status rather than waiting on one request.
// ---------------------------------------------------------------------------

let _btPrintPollTimer = null;
let _btPrintReady = false;

const _BT_PRINT_STATUS_TEXT = {
  not_set_up: ['Not set up', 'neutral'],
  installing: ['Setting up…', 'neutral'],
  ready: ['Ready', 'good'],
  failed: ['Failed', 'warn'],
  unsupported: ['Appliance only', 'neutral'],
};

function _btPrintSetPill(status) {
  const pill = document.getElementById('bt-print-pill');
  if (!pill) return;
  const [text, kind] = _BT_PRINT_STATUS_TEXT[status] || _BT_PRINT_STATUS_TEXT.not_set_up;
  pill.textContent = text;
  pill.className = 'set-pill set-pill-' + kind;
}

// Called when the Printing pane opens (no-op where the panel did not render,
// e.g. the wizard preview or a shape with no bt-print-pill).
function bluetoothPrintPaneOpen() {
  if (!document.getElementById('bt-print-pill')) return;
  loadBluetoothPrintStatus();
  if (_btPrintPollTimer) return;
  _btPrintPollTimer = setInterval(() => {
    const pane = document.getElementById('pane-printing');
    if (pane && pane.classList.contains('active')) loadBluetoothPrintStatus();
  }, 4000);
}

let _btPrintLastRender = null;

async function loadBluetoothPrintStatus() {
  const body = document.getElementById('bt-print-body');
  try {
    const r = await fetch('printing/bluetooth/status', { headers: { Accept: 'application/json' } });
    const d = await r.json().catch(() => ({}));
    const status = d.status || 'not_set_up';
    _btPrintSetPill(status);
    _btPrintReady = status === 'ready';
    // Re-render the step body only when the status changes (or a failure log
    // arrives), so a 4s poll never flickers or interrupts a click.
    const key = status + (status === 'failed' ? ':' + (d.log_tail || '').length : '');
    if (body && key !== _btPrintLastRender) {
      _btPrintLastRender = key;
      _btPrintRenderBody(body, status, d);
    }
  } catch (e) { /* transient; the next poll retries */ }
}

// Render the one step the user is on, so the panel reads set up, then turn on,
// then find, instead of a lone button that re-runs a long install.
function _btPrintRenderBody(body, status, d) {
  const sat = body.getAttribute('data-satellite') === 'true';
  const satNote = sat
    ? '<div class="small text-secondary mt-2"><i class="bi bi-diagram-3 me-1"></i>This printer works here and is shared to your main server, so either screen can print to it.</div>'
    : '';
  if (status === 'installing') {
    body.innerHTML = '<div class="text-secondary"><span class="spinner-border spinner-border-sm me-1"></span>Preparing the printer software on this device. The first setup can take several minutes; you can leave this page and come back.</div>';
  } else if (status === 'ready') {
    const rerun = '<button type="button" class="btn btn-link btn-sm text-secondary ms-1" onclick="bluetoothPrintSetup(this)">Run setup again</button>';
    if (sat) {
      // A satellite has no local Add printers panel (it prints through the main
      // server's shared printers), so send the user to the server to add it,
      // not to a Find button that would do nothing here.
      body.innerHTML =
        '<div class="text-success mb-2"><i class="bi bi-check-circle me-1"></i>The Bluetooth bridge is ready on this device.</div>'
        + '<div class="small mb-2">Turn your Supvan printer on nearby, then add it <strong>on your main server</strong>: open Settings, Printing, Find printers there, and add the Supvan printer shown as "(on this device)".</div>'
        + rerun;
    } else {
      body.innerHTML =
        '<div class="text-success mb-2"><i class="bi bi-check-circle me-1"></i>The Bluetooth bridge is ready on this device.</div>'
        + '<div class="small mb-2">Turn your Supvan printer on nearby, then find it and add it:</div>'
        + '<button type="button" class="btn btn-info btn-sm" onclick="bluetoothPrintFind()"><i class="bi bi-search me-1"></i>Find my printer</button>'
        + rerun;
    }
  } else if (status === 'failed') {
    const tail = d.log_tail ? `<pre class="small mt-2 mb-0 text-wrap">${_gEsc(String(d.log_tail).slice(-1500))}</pre>` : '';
    body.innerHTML =
      '<div class="text-danger mb-2"><i class="bi bi-x-circle-fill me-1"></i>Setup did not finish.</div>'
      + '<button type="button" class="btn btn-outline-info btn-sm" onclick="bluetoothPrintSetup(this)"><i class="bi bi-bluetooth me-1"></i>Try setup again</button>'
      + tail;
  } else {  // not_set_up
    body.innerHTML =
      '<div class="small text-secondary mb-2">Turn your Supvan printer on, then set up the Bluetooth bridge on this device so the printer joins the lists above.</div>'
      + '<button type="button" class="btn btn-outline-info btn-sm" onclick="bluetoothPrintSetup(this)"><i class="bi bi-bluetooth me-1"></i>Set up</button>'
      + satNote;
  }
}

async function bluetoothPrintSetup(btn) {
  const result = document.getElementById('bt-print-setup-result');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Setting up…'; }
  try {
    const r = await fetch('printing/bluetooth/setup', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if ((!r.ok || d.ok === false) && result) {
      result.innerHTML = `<span class="text-warning"><i class="bi bi-info-circle me-1"></i>${_gEsc(d.message || 'Could not start setup.')}</span>`;
    } else if (result) {
      result.innerHTML = '';
    }
  } catch (e) {
    if (result) result.innerHTML = `<span class="text-danger">Could not start setup: ${_gEsc(e.message)}</span>`;
  } finally {
    // Let the status drive the body from here (installing spinner, then ready).
    _btPrintLastRender = null;
    loadBluetoothPrintStatus();
  }
}

// From the Ready state: jump to Find printers and run it, so the two steps
// (prepare the bridge, then find the printer) connect instead of leaving the
// user to hunt for the separate Find printers box.
function bluetoothPrintFind() {
  const panel = document.getElementById('add-printer-panel');
  const btn = document.getElementById('find-printers-btn');
  if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (btn) findPrinters(btn);
}

// ---------------------------------------------------------------------------
// Thermometers pane (FoodAssistant-mnks). The live status, device lists, and
// the Home Assistant entity picker are all fed from /gadgets endpoints; the
// pane's Save posts only the two toggles. State polling runs while the pane
// is open (the interval no-ops when another pane is showing).
// ---------------------------------------------------------------------------

function _gEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function savePaneGadgets(btn) {
  const enabled = chk('gadgets_enabled');
  const haEnabled = chk('gadget_ha_enabled');
  return savePane({
    gadgets_enabled: enabled,
    gadget_ha_enabled: haEnabled,
  }, btn, 'gadgets-save-result').then(ok => {
    if (!ok) return;
    _gadgetsSetPill('gadgets-pill', enabled);
    _gadgetsSetPill('gadgets-ha-pill', haEnabled);
  });
}

// Beszel monitoring hub link (FoodAssistant-4kz2): saves the enable toggle
// and hub URL, then refreshes the dashboard link/pill immediately so the
// pane does not wait for the next resources poll.
function savePaneBeszel(btn) {
  const enabled = chk('beszel_enabled');
  return savePane({
    beszel_enabled: enabled,
    beszel_url: val('beszel_url'),
  }, btn, 'beszel-save-result').then(ok => {
    if (!ok) return;
    _gadgetsSetPill('beszel-pill', enabled);
    if (typeof loadResources === 'function') loadResources();
  });
}

function _gadgetsSetPill(id, on, textOn, textOff) {
  const pill = document.getElementById(id);
  if (!pill) return;
  pill.textContent = on ? (textOn || 'On') : (textOff || 'Off');
  pill.classList.toggle('set-pill-good', !!on);
  pill.classList.toggle('set-pill-neutral', !on);
  pill.classList.remove('set-pill-warn');
}

let _gadgetsPollTimer = null;
function gadgetsPaneOpen() {
  loadGadgetsState();
  loadGadgetsHaEntities();
  if (_gadgetsPollTimer) return;
  _gadgetsPollTimer = setInterval(() => {
    const pane = document.getElementById('pane-gadgets');
    if (pane && pane.classList.contains('active')) loadGadgetsState();
  }, 5000);
}

async function loadGadgetsState() {
  try {
    const r = await fetch('gadgets/state', { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    _gadgetsRender(await r.json());
  } catch (e) { /* transient; the next poll retries */ }
}

function _gadgetsFmtTemp(tempC, unit) {
  if (tempC == null) return '—';
  return unit === 'c' ? `${Math.round(tempC)}°C`
                      : `${Math.round(tempC * 9 / 5 + 32)}°F`;
}

function _gadgetsRender(d) {
  // Reader status line + pill, and the setup guidance while it never ran.
  const status = document.getElementById('gadgets-reader-status');
  const help = document.getElementById('gadgets-reader-help');
  const pill = document.getElementById('gadgets-reader-pill');
  const age = d.reader_age_seconds;
  if (age == null) {
    if (status) status.innerHTML = '<span class="text-secondary small">The reader has not checked in on this device yet.</span>';
    if (help) help.style.display = '';
    if (pill) { pill.textContent = 'Not set up'; pill.className = 'set-pill set-pill-neutral'; }
  } else if (age <= 90 && d.bluetooth_available === false) {
    // The reader is running but its Bluetooth radio is off or missing, so it
    // cannot see anything. Tell the user plainly instead of an empty list.
    if (status) status.innerHTML = '<span class="badge bg-warning text-dark me-2"><i class="bi bi-bluetooth me-1"></i>Bluetooth off</span><span class="text-secondary small">Bluetooth is turned off on this device. Turn it on, then thermometers in range appear below.</span>';
    if (help) help.style.display = 'none';
    if (pill) { pill.textContent = 'Bluetooth off'; pill.className = 'set-pill set-pill-warn'; }
  } else if (age <= 90) {
    if (status) status.innerHTML = `<span class="badge bg-success me-2"><i class="bi bi-check-circle me-1"></i>Reader connected</span><span class="text-secondary small">last update ${Math.round(age)}s ago</span>`;
    if (help) help.style.display = 'none';
    if (pill) { pill.textContent = 'Connected'; pill.className = 'set-pill set-pill-good'; }
  } else {
    if (status) status.innerHTML = `<span class="badge bg-warning text-dark me-2"><i class="bi bi-exclamation-circle me-1"></i>Reader quiet</span><span class="text-secondary small">last heard from ${Math.round(age / 60)} min ago; check the device and its Bluetooth</span>`;
    if (help) help.style.display = 'none';
    if (pill) { pill.textContent = 'Quiet'; pill.className = 'set-pill set-pill-warn'; }
  }

  // Configured thermometers: name (rename), per-probe role + temp + setpoint,
  // battery, remove.
  const devEl = document.getElementById('gadgets-devices');
  // Don't redraw over an edit in progress (an open role select or a focused
  // field inside the list), or the poll would wipe the user's interaction.
  if (devEl && !devEl.contains(document.activeElement)) {
    const devices = d.devices || [];
    if (!devices.length) {
      devEl.innerHTML = '<span class="text-secondary small">No thermometers added yet.</span>';
    } else {
      devEl.innerHTML = devices.map(dev => _gadgetsDeviceCard(dev, d.unit)).join('');
    }
  }

  // Discovered-but-unconfigured thermometers with an Add button.
  const discEl = document.getElementById('gadgets-discovered');
  if (discEl) {
    const found = d.discovered || [];
    if (!found.length) {
      discEl.innerHTML = '<span class="text-secondary small">Nothing new in range. Turn a thermometer on nearby and it appears here.</span>';
    } else {
      discEl.innerHTML = found.map(dev => {
        if (dev.supported === false) {
          // A probe-looking device with no decoder yet: show it so the user
          // knows the reader saw it, but there is nothing to add.
          return `<div class="d-flex align-items-center gap-2 small mb-1">
            <i class="bi bi-broadcast text-secondary"></i>
            <span><strong>${_gEsc(dev.name || dev.id)}</strong>
              <span class="text-secondary font-monospace">${_gEsc(dev.id)}</span>
              <span class="text-secondary">seen nearby, not supported yet</span></span>
          </div>`;
        }
        return `<div class="d-flex align-items-center gap-2 small mb-1">
          <i class="bi bi-broadcast text-info"></i>
          <span><strong>${_gEsc(dev.name || dev.id)}</strong>
            <span class="text-secondary font-monospace">${_gEsc(dev.id)}</span></span>
          <button type="button" class="btn btn-outline-info btn-sm ms-auto py-0"
                  onclick="gadgetsAddDiscovered('${_gEsc(dev.id)}', '${_gEsc(dev.name || '')}', '${_gEsc(dev.protocol || '')}')">
            <i class="bi bi-plus-lg me-1"></i>Add
          </button>
        </div>`;
      }).join('');
    }
  }
}

// One configured thermometer: a header (name, rename, battery, remove) over a
// row per probe (role, live temp, setpoint).
function _gadgetsDeviceCard(dev, unit) {
  const probes = dev.probes || [];
  const isHa = dev.protocol === 'home_assistant';
  const rows = probes.length
    ? probes.map(p => _gadgetsProbeRow(dev, p, unit)).join('')
    : `<div class="text-secondary small">${dev.stale ? 'not reporting' : 'no probe readings yet'}</div>`;
  const meta = [];
  if (isHa) meta.push('via Home Assistant');
  // Battery: a low reading (from the same threshold every surface uses) shows
  // its own red badge, so it is not lost in the grey meta line (FoodAssistant-oyt9).
  if (dev.battery != null && !dev.battery_low) meta.push(`battery ${dev.battery}%`);
  if (dev.stale && probes.length) meta.push('last seen a while ago');
  const batteryBadge = (dev.battery != null && dev.battery_low)
    ? `<span class="badge bg-danger ms-1" title="Battery is low"><i class="bi bi-battery me-1"></i>Battery ${dev.battery}%</span>`
    : '';
  return `<div class="border rounded p-2 mb-2">
    <div class="d-flex align-items-center gap-2">
      <i class="bi bi-thermometer-half ${dev.stale ? 'text-secondary' : 'text-success'}"></i>
      <strong>${_gEsc(dev.name || dev.id)}</strong>
      <button type="button" class="btn btn-link btn-sm p-0 text-secondary"
              onclick="gadgetsRenameDevice('${_gEsc(dev.id)}', '${_gEsc(dev.name || '')}')"
              title="Rename this thermometer"><i class="bi bi-pencil"></i></button>
      ${batteryBadge}
      ${meta.length ? `<span class="text-secondary small ms-1">${_gEsc(meta.join(' · '))}</span>` : ''}
      <button type="button" class="btn btn-outline-danger btn-sm ms-auto py-0"
              onclick="gadgetsRemoveDevice('${_gEsc(dev.id)}')" title="Remove this thermometer">
        <i class="bi bi-trash3"></i>
      </button>
    </div>
    <div class="ms-4 mt-1">${rows}</div>
  </div>`;
}

// A probe line: a role selector (Auto/Internal/Ambient/Food), the live
// temperature, and the setpoint, which prefers the user's own target and falls
// back to one the device itself broadcasts (a Govee grill alarm).
function _gadgetsProbeRow(dev, p, unit) {
  const roleOpts = ['', 'internal', 'ambient', 'food'].map(r => {
    const label = r === '' ? 'Auto' : (r.charAt(0).toUpperCase() + r.slice(1));
    const sel = (p.role_source === 'you' ? p.role : '') === r ? ' selected' : '';
    return `<option value="${r}"${sel}>${label}</option>`;
  }).join('');
  const roleSel = `<select class="form-select form-select-sm py-0 w-auto d-inline-block"
      style="min-width:6.5rem" onchange="gadgetsSetProbeRole('${_gEsc(dev.id)}', ${p.index}, this.value)"
      title="What this probe measures">${roleOpts}</select>`;
  const roleTag = p.role_label
    ? `<span class="text-secondary small">(${_gEsc(p.role_label)}${p.role_source === 'auto' ? ', auto' : ''})</span>`
    : '';
  const temp = _gadgetsFmtTemp(p.temp_c, unit);
  let setpoint = '';
  if (p.target_c != null) {
    setpoint = `<span class="text-info small">${p.direction === 'below' ? '↓' : '↑'} target ${_gadgetsFmtTemp(p.target_c, unit)}</span>`;
  } else if (p.device_target_c != null) {
    setpoint = `<span class="text-secondary small">device target ${_gadgetsFmtTemp(p.device_target_c, unit)}</span>`;
  }
  return `<div class="d-flex align-items-center gap-2 small mb-1">
    <span class="text-secondary" style="min-width:3.5rem">Probe ${p.index}</span>
    ${roleSel}${roleTag}
    <span class="fw-semibold">${temp}</span>
    ${setpoint}
  </div>`;
}

async function gadgetsRenameDevice(id, current) {
  const name = window.prompt('Name for this thermometer (e.g. Grill, Smoker):', current || '');
  if (name === null) return;   // cancelled
  try {
    await _gadgetsPost('gadgets/name', { device_id: id, name: name });
    loadGadgetsState();
  } catch (e) { /* the list re-renders on the next poll */ }
}

async function gadgetsSetProbeRole(id, probe, role) {
  try {
    await _gadgetsPost('gadgets/probe-role', { device_id: id, probe: probe, role: role });
    loadGadgetsState();
  } catch (e) { /* the list re-renders on the next poll */ }
}

async function _gadgetsPost(url, body, method) {
  const r = await fetch(url, {
    method: method || 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || d.detail || d.message || r.statusText);
  return d;
}

async function gadgetsAddDiscovered(id, name, protocol) {
  try {
    await _gadgetsPost('gadgets/devices', { id, name, protocol });
    document.getElementById('gadgets_enabled').checked = true;
    _gadgetsSetPill('gadgets-pill', true);
    loadGadgetsState();
  } catch (e) { /* the list re-renders on the next poll */ }
}

async function gadgetsRemoveDevice(id) {
  try {
    await _gadgetsPost('gadgets/devices/' + encodeURIComponent(id), null, 'DELETE');
    loadGadgetsState();
  } catch (e) { /* the list re-renders on the next poll */ }
}

async function gadgetsAddManual(btn) {
  const el = document.getElementById('gadget-add-result');
  const id = val('gadget-add-id');
  if (!id) { if (el) el.innerHTML = '<span class="text-danger">Enter the thermometer\'s address first.</span>'; return; }
  btn.disabled = true;
  try {
    await _gadgetsPost('gadgets/devices', {
      id, name: val('gadget-add-name'), protocol: val('gadget-add-protocol'),
    });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Added. It reports as soon as the reader sees it.</span>';
    document.getElementById('gadget-add-id').value = '';
    document.getElementById('gadget-add-name').value = '';
    document.getElementById('gadgets_enabled').checked = true;
    _gadgetsSetPill('gadgets-pill', true);
    loadGadgetsState();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${_gEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

// One-click reader install (Pi appliances; the server pane shows manual steps
// instead). Mirrors installPrintStack.
async function installGadgetsReader(btn) {
  const el = document.getElementById('gadgets-install-result');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Setting up the reader…';
  if (el) el.innerHTML = '<span class="text-secondary">This can take a couple of minutes.</span>';
  try {
    const r = await fetch('gadgets/install', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    const msg = _gEsc(d.message || d.detail || r.statusText || '').replace(/\n/g, '<br>');
    if (r.ok && d.ok) {
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${msg}</span>`;
      loadGadgetsState();
    } else {
      if (el) el.innerHTML = `<span class="text-warning"><i class="bi bi-info-circle me-1"></i>${msg}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>Could not set up the reader: ${_gEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// -- Home Assistant source ---------------------------------------------------

const GADGET_HA_HIDE_EMPTY_KEY = 'gadgetHaHideEmpty';

function _gadgetsHaHideEmpty() {
  try { return localStorage.getItem(GADGET_HA_HIDE_EMPTY_KEY) !== 'false'; } catch (e) { return true; }
}

let _gadgetsHaEntities = [];
let _gadgetsHaGrouped = [];

function _gadgetsHaPopulateList() {
  const list = document.getElementById('gadget-ha-entity-list');
  if (!list) return;
  const entities = _gadgetsHaHideEmpty()
    ? _gadgetsHaEntities.filter(e => e.has_value)
    : _gadgetsHaEntities;
  list.innerHTML = entities.map(e =>
    `<option value="${_gEsc(e.entity_id)}">${_gEsc(e.name)} (${_gEsc(e.state)}${_gEsc(e.unit)})</option>`).join('');
}

function gadgetsHaToggleHideEmpty(checkbox) {
  try { localStorage.setItem(GADGET_HA_HIDE_EMPTY_KEY, checkbox.checked ? 'true' : 'false'); } catch (e) { }
  _gadgetsHaPopulateList();
}

async function loadGadgetsHaEntities() {
  try {
    const r = await fetch('gadgets/ha-entities', { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const d = await r.json();
    const toggle = document.getElementById('gadget-ha-hide-empty');
    if (toggle) toggle.checked = _gadgetsHaHideEmpty();
    if (d.connected) {
      _gadgetsHaEntities = d.entities || [];
      _gadgetsHaGrouped = d.grouped || [];
      _gadgetsHaPopulateList();
    }
    _gadgetsHaRenderList(d.configured || []);
    _gadgetsHaRenderDiscover();
  } catch (e) { /* the picker degrades to a plain text field */ }
}

// Groups of related probe entities (from a grill or multi-probe
// thermometer) that HA already knows about but the user has not added yet,
// so "Add all" beats hunting each probe one at a time.
function _gadgetsHaRenderDiscover() {
  const wrap = document.getElementById('gadget-ha-discover');
  const btn = document.getElementById('gadget-ha-discover-btn');
  if (!wrap) return;
  const configured = new Set(
    (document.getElementById('gadget-ha-list') ? _gadgetsHaConfiguredIds() : []));
  const groups = (_gadgetsHaGrouped || [])
    .map(g => ({ ...g, entity_ids: (g.entity_ids || []).filter(id => !configured.has(id)) }))
    .filter(g => g.entity_ids.length > 1);
  if (btn) btn.classList.toggle('d-none', !groups.length);
  if (!groups.length) { wrap.innerHTML = ''; wrap.classList.add('d-none'); return; }
  wrap.classList.remove('d-none');
  wrap.innerHTML = groups.map((g, i) => `<div class="d-flex align-items-center gap-2 small mb-1">
      <i class="bi bi-thermometer-half text-secondary"></i>
      <span>${_gEsc(g.device_name)}</span>
      <span class="text-secondary">(${g.entity_ids.length} probes)</span>
      <button type="button" class="btn btn-outline-info btn-sm ms-auto py-0"
              onclick="gadgetsHaAddGroup(${i}, this)">
        <i class="bi bi-plus-lg me-1"></i>Add all
      </button>
    </div>`).join('');
  wrap.dataset.groups = JSON.stringify(groups);
}

function _gadgetsHaConfiguredIds() {
  const el = document.getElementById('gadget-ha-list');
  if (!el) return [];
  return Array.from(el.querySelectorAll('.font-monospace')).map(n => n.textContent.trim());
}

function gadgetsHaToggleDiscover() {
  const wrap = document.getElementById('gadget-ha-discover');
  if (wrap) wrap.classList.toggle('d-none');
}

async function gadgetsHaAddGroup(index, btn) {
  const wrap = document.getElementById('gadget-ha-discover');
  const el = document.getElementById('gadget-ha-add-result');
  let groups = [];
  try { groups = JSON.parse(wrap.dataset.groups || '[]'); } catch (e) { /* ignore */ }
  const group = groups[index];
  if (!group) return;
  const byId = {};
  _gadgetsHaEntities.forEach(e => { byId[e.entity_id] = e.name; });
  const entities = group.entity_ids.map(id => ({ entity_id: id, name: byId[id] || '' }));
  btn.disabled = true;
  try {
    const d = await _gadgetsPost('gadgets/ha-devices',
      { device_name: group.device_name, entities });
    if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Added ${d.added} probes from ${_gEsc(group.device_name)}.</span>`;
    _gadgetsHaRenderList(d.entities || []);
    document.getElementById('gadgets_enabled').checked = true;
    document.getElementById('gadget_ha_enabled').checked = true;
    _gadgetsSetPill('gadgets-pill', true);
    _gadgetsSetPill('gadgets-ha-pill', true);
    loadGadgetsHaEntities();
    loadGadgetsState();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${_gEsc(e.message)}</span>`;
    btn.disabled = false;
  }
}

function _gadgetsHaRenderList(entities) {
  const el = document.getElementById('gadget-ha-list');
  if (!el) return;
  if (!entities.length) {
    el.innerHTML = '<span class="text-secondary small">No entities added yet.</span>';
    return;
  }
  el.innerHTML = entities.map(entity => `<div class="d-flex align-items-center gap-2 small mb-1">
      <i class="bi bi-thermometer-half text-secondary"></i>
      <span class="font-monospace">${_gEsc(entity)}</span>
      <button type="button" class="btn btn-outline-danger btn-sm ms-auto py-0"
              onclick="gadgetsHaRemove('${_gEsc(entity)}')" title="Stop reading this entity">
        <i class="bi bi-trash3"></i>
      </button>
    </div>`).join('');
}

async function gadgetsHaAdd(btn) {
  const el = document.getElementById('gadget-ha-add-result');
  const entity = val('gadget-ha-entity');
  if (!entity) { if (el) el.innerHTML = '<span class="text-danger">Enter an entity id first.</span>'; return; }
  btn.disabled = true;
  try {
    const input = document.getElementById('gadget-ha-entity');
    // When the entity was picked from the datalist, its friendly name rides
    // along as the device name.
    const opt = document.querySelector(`#gadget-ha-entity-list option[value="${CSS.escape(entity)}"]`);
    const name = opt ? (opt.textContent || '').replace(/\s*\(.*\)$/, '') : '';
    const d = await _gadgetsPost('gadgets/ha-entities', { entity_id: entity, name });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Added. Readings appear within a few seconds.</span>';
    input.value = '';
    _gadgetsHaRenderList(d.entities || []);
    document.getElementById('gadgets_enabled').checked = true;
    document.getElementById('gadget_ha_enabled').checked = true;
    _gadgetsSetPill('gadgets-pill', true);
    _gadgetsSetPill('gadgets-ha-pill', true);
    loadGadgetsState();
    _gadgetsHaRenderDiscover();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${_gEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

async function gadgetsHaRemove(entity) {
  try {
    const d = await _gadgetsPost('gadgets/ha-entities/' + encodeURIComponent(entity), null, 'DELETE');
    _gadgetsHaRenderList(d.entities || []);
    loadGadgetsState();
    _gadgetsHaRenderDiscover();
  } catch (e) { /* the list re-renders on the next open */ }
}

// ---- ESPHome WiFi sensor source (FoodAssistant-0oq3) --------------------

function _gadgetsEspRenderList(devices) {
  const box = document.getElementById('gadget-esp-list');
  if (!box) return;
  if (!devices || !devices.length) {
    box.innerHTML = '<span class="text-secondary small">No ESP devices added yet.</span>';
    return;
  }
  box.innerHTML = '';
  devices.forEach(function (dev) {
    const id = 'ESP:' + String(dev.host || '').toUpperCase() + ':' + String(dev.sensor || '').toUpperCase();
    const row = document.createElement('div');
    row.className = 'd-flex align-items-center gap-2 small mb-1';
    const icon = document.createElement('i');
    icon.className = 'bi bi-cpu text-secondary';
    const nameEl = document.createElement('span');
    nameEl.textContent = dev.name || dev.sensor || '';
    const addr = document.createElement('span');
    addr.className = 'font-monospace text-secondary';
    addr.textContent = (dev.host || '') + '/' + (dev.sensor || '');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-outline-danger btn-sm ms-auto py-0';
    btn.title = 'Stop reading this sensor';
    btn.innerHTML = '<i class="bi bi-trash3"></i>';
    btn.addEventListener('click', function () { gadgetsEspRemove(id); });
    row.append(icon, nameEl, addr, btn);
    box.appendChild(row);
  });
}

async function gadgetsEspDiscover(btn) {
  const el = document.getElementById('gadget-esp-add-result');
  const host = val('gadget-esp-host');
  if (!host) { if (el) el.innerHTML = '<span class="text-danger">Enter the device address first.</span>'; return; }
  btn.disabled = true;
  if (el) el.innerHTML = '<span class="text-secondary"><i class="bi bi-hourglass-split me-1"></i>Looking for sensors…</span>';
  try {
    const r = await fetch('gadgets/esp-sensors?host=' + encodeURIComponent(host),
                          { headers: { Accept: 'application/json' } });
    const d = await r.json().catch(() => ({}));
    if (d.ok === false && d.error) throw new Error(d.error);
    const list = document.getElementById('gadget-esp-sensor-list');
    if (list) {
      list.innerHTML = '';
      (d.sensors || []).forEach(function (s) {
        const opt = document.createElement('option');
        opt.value = s.sensor;
        opt.textContent = (s.name || s.sensor) + (s.state ? ' (' + s.state + ')' : '');
        list.appendChild(opt);
      });
    }
    if (d.sensors && d.sensors.length) {
      if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Found ' + d.sensors.length + ' sensor(s). Pick one below.</span>';
      // Prefill the first sensor for the common single-probe case.
      const sInput = document.getElementById('gadget-esp-sensor');
      if (sInput && !sInput.value) sInput.value = d.sensors[0].sensor;
    } else {
      if (el) el.innerHTML = '<span class="text-warning">No temperature sensors found. Check the address, or type the sensor id yourself.</span>';
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${_gEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

async function gadgetsEspAdd(btn) {
  const el = document.getElementById('gadget-esp-add-result');
  const host = val('gadget-esp-host');
  const sensor = val('gadget-esp-sensor');
  const name = val('gadget-esp-name');
  if (!host || !sensor) { if (el) el.innerHTML = '<span class="text-danger">Enter the device address and a sensor id first.</span>'; return; }
  btn.disabled = true;
  try {
    const d = await _gadgetsPost('gadgets/esp-devices', { host, sensor, name });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Added. Readings appear within a few seconds.</span>';
    document.getElementById('gadget-esp-sensor').value = '';
    document.getElementById('gadget-esp-name').value = '';
    _gadgetsEspRenderList(d.esp_devices || []);
    document.getElementById('gadgets_enabled').checked = true;
    document.getElementById('gadget_esp_enabled').checked = true;
    _gadgetsSetPill('gadgets-pill', true);
    _gadgetsSetPill('gadgets-esp-pill', true);
    loadGadgetsState();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${_gEsc(e.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

async function gadgetsEspRemove(deviceId) {
  try {
    const d = await _gadgetsPost('gadgets/esp-devices/' + encodeURIComponent(deviceId), null, 'DELETE');
    _gadgetsEspRenderList(d.esp_devices || []);
    loadGadgetsState();
  } catch (e) { /* the list re-renders on the next open */ }
}
