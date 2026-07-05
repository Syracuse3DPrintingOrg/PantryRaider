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
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
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
  if (!confirm('Unlink this install from Forager? AI calls through the subscription will stop until it is linked again.')) return;
  const el = document.getElementById('cloud-link-result');
  btn.disabled = true;
  try {
    await fetch('setup/cloud/unlink', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Unlinked. Reloading…</span>';
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
    let html = '<i class="bi bi-cloud-check me-1"></i>Linked as <strong>' +
      String(d.name || 'this install').replace(/[<>&"]/g, '') + '</strong>';
    html += ent.active
      ? ' · ' + (ent.plan ? String(ent.plan).replace(/[<>&"]/g, '') + ' plan, ' : '') + 'subscription active'
      : ' · <span class="text-warning">no active subscription</span>';
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
          (ent.active ? 'no monthly quota set for this plan.' : 'no active subscription.');
      }
    }
  } catch (e) { fail('Forager could not be reached.'); }
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
    themealdb_api_key: document.getElementById('themealdb_api_key') ? secretVal('themealdb_api_key') : undefined,
    spoonacular_api_key: document.getElementById('spoonacular_api_key') ? secretVal('spoonacular_api_key') : undefined,
  }, btn, 'recipes-save-result');
}

// Recipe suggestion tuning + the kitchen appliances checklist: the Suggestion
// Tuning card of the Recipe Preferences pane, saved by its own button.
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
    viewer_password: document.getElementById('viewer_password') ? secretVal('viewer_password') : undefined,
    api_key:       document.getElementById('api_key') ? secretVal('api_key') : undefined,
    kiosk_pin:     document.getElementById('kiosk_pin') ? secretVal('kiosk_pin') : undefined,
    kiosk_readonly_when_locked: chk('kiosk_readonly_when_locked'),
    // The Add satellite key rows live in this pane, so its Save must post them or
    // a newly added satellite key is collected but never persisted (the satellite
    // then gets a 401 because the server never stored its key).
    extra_api_keys: document.querySelector('.satellite-extra-keys') ? collectSatelliteKeys() : undefined,
  }, btn, 'security-save-result');
}

// The Remote Access (tunnel) pane has no standalone Save: its persisted fields
// (tunnel_mode, tunnel_token) are written by Connect/Disconnect via tunnel/start
// and tunnel/stop, which also start or stop the tunnel container.

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

async function verifyTOTP() {
  const code = document.getElementById('totp-code').value.trim();
  if (!_totpSecret || !code) return;
  const d = await postJson('setup/totp/verify', {secret: _totpSecret, code});
  const el = document.getElementById('totp-result');
  if (d.ok) {
    el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>' + d.message + '</span>';
    setTimeout(() => location.reload(), 1500);
  } else {
    el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>' + d.error + '</span>';
  }
}

async function disableTOTP() {
  if (!confirm('Disable two-factor authentication? Anyone with the UI password can log in without a code.')) return;
  const d = await postJson('setup/totp/disable', {});
  if (d.ok) location.reload();
  else alert(d.error || 'Failed');
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
    const fd = new FormData();
    fd.append('file', f);
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

function tunnelModeChanged() {
  const mode = document.querySelector('input[name="tunnel_mode"]:checked')?.value || '';
  const tokenSection = document.getElementById('tunnel-token-section');
  const buttons = document.getElementById('tunnel-buttons');
  const helpCF = document.getElementById('tunnel-help-cloudflare');
  const helpSub = document.getElementById('tunnel-help-subscription');
  const label = document.getElementById('tunnel-token-label');

  if (!tokenSection) return;

  if (mode === '') {
    tokenSection.style.display = 'none';
    if (buttons) buttons.style.setProperty('display', 'none', 'important');
  } else {
    tokenSection.style.display = '';
    if (buttons) buttons.style.removeProperty('display');
    if (mode === 'cloudflare') {
      if (label) label.textContent = 'Cloudflare Tunnel Token';
      if (helpCF) helpCF.style.display = '';
      if (helpSub) helpSub.style.display = 'none';
    } else {
      if (label) label.textContent = 'Subscription Token';
      if (helpCF) helpCF.style.display = 'none';
      if (helpSub) helpSub.style.display = '';
    }
  }
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
