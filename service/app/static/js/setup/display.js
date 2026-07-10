// Appliance-only: Display pane

// The kiosk only needs a restart when the display SCALE (zoom) changes; it
// re-applies the zoom on the next load. Screensaver, touch, idle, and the other
// fields saved by this card do not need a restart. The card used to ALWAYS
// restart the kiosk, so changing only a screensaver setting flashed the boot
// splash on the panel (FoodAssistant-b0by). Capture the scale at load and only
// restart when it actually changed. A scale change made with the dropdown
// already restarts through applyDisplay's onchange; this just guards the Save
// button's own restart. Capture now if the control is present (deferred load),
// else on DOMContentLoaded (head load).
let _faLoadedUiScale = (function () {
  try { return document.getElementById('ui_scale') ? document.getElementById('ui_scale').value : null; }
  catch (e) { return null; }
})();
document.addEventListener('DOMContentLoaded', function () {
  if (_faLoadedUiScale === null) {
    const el = document.getElementById('ui_scale');
    if (el) _faLoadedUiScale = el.value;
  }
});

async function saveDisplaySettings() {
  const el = document.getElementById('display-save-result');
  if (el) el.innerHTML = '<span class="text-secondary">Saving...</span>';
  const ui_scale = document.getElementById('ui_scale')?.value || 'normal';
  // On the Pi the orientation lives in the Orientation card (kms_rotation), the
  // only rotation control here, so read it; otherwise saving scale would reset
  // the saved rotation to 0. Fall back to the non-Pi select when present.
  const display_rotation = parseInt(
    document.getElementById('kms_rotation')?.value
    ?? document.getElementById('display_rotation')?.value ?? '0', 10);
  const display_touch = document.getElementById('display_touch')?.checked || false;
  const display_type = document.getElementById('display_type')?.value || 'generic';
  const display_idle_timeout = parseInt(document.getElementById('display_idle_timeout')?.value || '0', 10);
  const screensaver_minutes = parseInt(document.getElementById('screensaver_minutes')?.value || '0', 10);
  const screensaver_speed = document.getElementById('screensaver_speed')?.value || 'normal';
  const screensaver_pill_scale = document.getElementById('screensaver_pill_scale')?.value || 'normal';
  const screensaver_mode = document.getElementById('screensaver_mode')?.value || 'bounce';
  const screensaver_photo_seconds = parseInt(document.getElementById('screensaver_photo_seconds')?.value, 10) || 25;
  const screensaver_ken_burns = document.getElementById('screensaver_ken_burns') ? document.getElementById('screensaver_ken_burns').checked : true;
  const screensaver_ken_burns_speed = document.getElementById('screensaver_ken_burns_speed')?.value || 'normal';
  const screensaver_all_clients = document.getElementById('screensaver_all_clients')?.checked || false;
  const osk_enabled = document.getElementById('osk_enabled')?.checked ?? true;
  const wake_on_motion = document.getElementById('wake_on_motion')?.value || 'auto';
  try {
    await fetch('setup/scale', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ui_scale, display_rotation}),
    });
    const saveResp = await (await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({display_touch, display_type, display_idle_timeout, screensaver_minutes, screensaver_speed, screensaver_pill_scale, screensaver_mode, screensaver_photo_seconds, screensaver_ken_burns, screensaver_ken_burns_speed, screensaver_all_clients, osk_enabled, wake_on_motion, ...photoSourceFields()}),
    })).json();
    // Only restart the kiosk when the display scale actually changed; a
    // screensaver-only (or touch/idle/other) change does not need it and would
    // otherwise flash the boot splash (FoodAssistant-b0by).
    if (_faLoadedUiScale === null || ui_scale !== _faLoadedUiScale) {
      fetch('setup/kiosk/restart', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).catch(() => {});
    }
    _faLoadedUiScale = ui_scale;
    if (saveResp && saveResp.touch_needs_reboot) {
      // The touch overlay was written but only loads on reboot; don't reload the
      // page (that would hide this), let the user reboot when ready.
      if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Display settings saved. ' +
        'Touch overlay written &mdash; <strong>reboot to activate touch</strong>. ' +
        '<button type="button" class="btn btn-outline-secondary btn-sm ms-2" onclick="applyTouchDriver(this, true)">' +
        '<i class="bi bi-arrow-repeat me-1"></i>Reboot now</button></span>';
      return;
    }
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Display settings saved.</span>';
    location.reload();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

// Start the screensaver on this screen right now with the style and speed
// currently picked in the form, so choices can be previewed without waiting
// for the idle timeout (FoodAssistant-fiwc). Any touch or key dismisses it.
async function saveScreensaverSettings() {
  // The off-Pi Screensaver card: persist just its own fields, so this save
  // can never clobber unrelated display settings.
  const el = document.getElementById('screensaver-save-result');
  const body = {
    screensaver_minutes: parseInt(document.getElementById('screensaver_minutes')?.value || '0', 10),
    screensaver_speed: document.getElementById('screensaver_speed')?.value || 'normal',
    screensaver_pill_scale: document.getElementById('screensaver_pill_scale')?.value || 'normal',
    screensaver_mode: document.getElementById('screensaver_mode')?.value || 'bounce',
    screensaver_photo_seconds: parseInt(document.getElementById('screensaver_photo_seconds')?.value, 10) || 25,
    screensaver_ken_burns: document.getElementById('screensaver_ken_burns') ? document.getElementById('screensaver_ken_burns').checked : true,
    screensaver_ken_burns_speed: document.getElementById('screensaver_ken_burns_speed')?.value || 'normal',
    screensaver_all_clients: document.getElementById('screensaver_all_clients')?.checked || false,
    osk_enabled: document.getElementById('osk_enabled')?.checked ?? true,
    ...photoSourceFields(),
  };
  try {
    const r = await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Screensaver settings saved.</span>';
  } catch (e) {
    if (el) el.innerHTML = '<span class="text-danger"><i class="bi bi-x-circle me-1"></i>Could not save: ' + e + '</span>';
  }
}

function testScreensaver() {
  const el = document.getElementById('display-save-result');
  if (typeof window.__screensaverTest !== 'function') {
    if (el) el.innerHTML = '<span class="text-warning">The screensaver script has not loaded on this page; reload and try again.</span>';
    return;
  }
  if (el) el.innerHTML = '';
  window.__screensaverTest({
    speed: document.getElementById('screensaver_speed')?.value,
    kenBurnsSpeed: document.getElementById('screensaver_ken_burns_speed')?.value,
    mode: document.getElementById('screensaver_mode')?.value,
  });
}

async function saveFloatingNav() {
  const el = document.getElementById('floating-nav-result');
  if (el) el.innerHTML = '<span class="text-secondary">Saving...</span>';
  const floating_nav_position = document.getElementById('floating_nav_position')?.value || 'off';
  const floating_nav_autohide_streamdeck = document.getElementById('floating_nav_autohide_streamdeck')?.checked || false;
  const nav_visibility = document.getElementById('nav_visibility')?.value || 'auto';
  const timer_chips = document.getElementById('timer_chips')?.value || 'auto';
  const quiet_mode = document.getElementById('quiet_mode')?.checked || false;
  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({floating_nav_position, floating_nav_autohide_streamdeck, nav_visibility, timer_chips, quiet_mode}),
    });
    // Clear any per-device override so the new default dock takes effect here.
    try { localStorage.removeItem('floatNavPosition'); } catch (e) {}
    try { localStorage.removeItem('floatNavOrientation'); } catch (e) {}
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Navigation menu saved.</span>';
    location.reload();
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

async function loadDisplayStatus() {
  const el = document.getElementById('kms-rotation-status');
  if (!el) return;
  try {
    const r = await fetch('setup/display/rotation');
    const d = await r.json();
    if (d.ok) {
      el.innerHTML = `<span class="text-secondary small">Current KMS rotation: <strong>${d.rotation}&deg;</strong></span>`;
      const sel = document.getElementById('kms_rotation');
      if (sel) sel.value = String(d.rotation);
    } else {
      el.innerHTML = `<span class="text-secondary small">Could not read KMS rotation (${d.error})</span>`;
    }
  } catch (e) {
    el.innerHTML = '<span class="text-secondary small">Host bridge unavailable.</span>';
  }
}

async function setKmsRotation(reboot) {
  const degrees = parseInt(document.getElementById('kms_rotation')?.value || '0', 10);
  const el = document.getElementById('kms-rotation-result');
  if (el) el.innerHTML = '<span class="text-secondary">Applying...</span>';
  try {
    const r = await fetch('setup/display/rotation', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({degrees, reboot}),
    });
    const d = await r.json();
    if (d.ok) {
      const msg = reboot ? 'Rotation applied. Rebooting...' : `KMS rotation set to ${degrees}&deg;. Reboot to activate.`;
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>${msg}</span>`;
    } else {
      if (el) el.innerHTML = `<span class="text-danger">${d.error}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

// Appliance-only: Stream Deck pane

// Write the boot config the selected display type needs (e.g. enable SPI and
// add the ADS7846 overlay for a resistive HDMI panel). This is what firstboot
// would have done had the display type been known at first boot; running it
// here covers a type chosen later in the wizard. A reboot loads a new overlay.
async function applyTouchDriver(btn, reboot) {
  const el = document.getElementById('touch-provision-result');
  const dtype = document.getElementById('display_type')?.value || 'generic';
  if (el) { el.className = 'test-result mt-2 text-info'; el.textContent = 'Applying...'; }
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/setup/touch/provision', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_type: dtype, reboot: !!reboot }),
    });
    const data = await r.json();
    if (!data.ok) {
      if (el) { el.className = 'test-result mt-2 text-danger'; el.textContent = 'Failed: ' + (data.error || 'unknown error'); }
    } else if (reboot && data.needs_reboot) {
      if (el) { el.className = 'test-result mt-2 text-success'; el.textContent = 'Overlay written. Rebooting now to load it...'; }
    } else if (data.needs_reboot) {
      if (el) { el.className = 'test-result mt-2 text-success'; el.innerHTML = 'Overlay written. <strong>Reboot the Pi</strong> to load it, then calibrate.'; }
    } else if (data.changed === false && (data.driver === 'ads7846')) {
      if (el) { el.className = 'test-result mt-2 text-success'; el.textContent = 'Already applied. Touch should be active (calibrate if taps are offset).'; }
    } else {
      if (el) { el.className = 'test-result mt-2 text-secondary'; el.textContent = 'No boot overlay needed for this display type.'; }
    }
  } catch (e) {
    if (el) { el.className = 'test-result mt-2 text-danger'; el.textContent = 'Request failed: ' + e; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Touch calibration is performed on the Pi's own display: clicking here sets a
// server flag, and the kiosk (which polls for it) navigates its screen to the
// fullscreen tap test. The person calibrating stands at the device.
async function startTouchCalibration() {
  const el = document.getElementById('touch-calibrate-result');
  if (el) { el.className = 'test-result mt-2 text-info'; el.textContent = 'Starting...'; }
  try {
    const r = await fetch('/setup/calibrate/touch/request', { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      if (el) {
        el.className = 'test-result mt-2 text-success';
        // The Cancel button lives HERE, on the remote browser, because the Pi
        // panel is uncalibrated during the test and hard to tap accurately.
        el.innerHTML = 'Calibration started on the Pi touchscreen. Walk to the device and tap each crosshair in turn.' +
          ' <button type="button" class="btn btn-outline-danger btn-sm ms-2" onclick="cancelTouchCalibration()">' +
          '<i class="bi bi-x-circle me-1"></i>Cancel calibration</button>';
      }
      _watchCalibrationDone();
    } else if (el) {
      el.className = 'test-result mt-2 text-danger';
      el.textContent = 'Could not start: ' + (data.error || 'unknown error');
    }
  } catch (e) {
    if (el) { el.className = 'test-result mt-2 text-danger'; el.textContent = 'Request failed: ' + e; }
  }
}

// Remove the stored calibration matrix and revert touch to the panel default.
async function resetTouchCalibration() {
  const el = document.getElementById('touch-calibrate-result');
  if (el) { el.className = 'test-result mt-2 text-info'; el.textContent = 'Resetting...'; }
  try {
    const r = await fetch('/setup/calibrate/touch/reset', { method: 'POST' });
    const data = await r.json();
    if (el) {
      el.className = 'test-result mt-2 ' + (data.ok ? 'text-success' : 'text-danger');
      el.textContent = data.ok
        ? 'Calibration reset to the panel default. The kiosk display restarts to apply it.'
        : 'Reset failed: ' + (data.error || 'unknown error');
    }
  } catch (e) {
    if (el) { el.className = 'test-result mt-2 text-danger'; el.textContent = 'Reset failed: ' + e; }
  }
}

// Poll until the calibration is applied on the Pi, then clear the in-progress
// state (including the Cancel button) so it does not linger after success. The
// Pi kiosk restarts when it applies, so it cannot report back itself; the app
// sets a one-shot done flag we watch here. Gives up after a few minutes.
let _calDoneTimer = null;
function _watchCalibrationDone() {
  if (_calDoneTimer) clearInterval(_calDoneTimer);
  const started = Date.now();
  _calDoneTimer = setInterval(async function () {
    if (Date.now() - started > 240000) { clearInterval(_calDoneTimer); _calDoneTimer = null; return; }
    try {
      const r = await fetch('/setup/calibrate/touch/done/pending', { cache: 'no-store' });
      const d = await r.json();
      if (d && d.pending) {
        clearInterval(_calDoneTimer); _calDoneTimer = null;
        const el = document.getElementById('touch-calibrate-result');
        if (el) { el.className = 'test-result mt-2 text-success'; el.textContent = 'Calibration applied. The kiosk display restarted to load it.'; }
      }
    } catch (e) { /* transient: keep polling */ }
  }, 1500);
}

// Cancel a calibration in progress from this remote browser: the Pi's
// fullscreen calibration page polls for this and returns to the dashboard.
async function cancelTouchCalibration() {
  const el = document.getElementById('touch-calibrate-result');
  if (_calDoneTimer) { clearInterval(_calDoneTimer); _calDoneTimer = null; }
  try {
    await fetch('/setup/calibrate/touch/cancel', { method: 'POST' });
    if (el) { el.className = 'test-result mt-2 text-secondary'; el.textContent = 'Calibration cancelled.'; }
  } catch (e) {
    if (el) { el.className = 'test-result mt-2 text-danger'; el.textContent = 'Cancel failed: ' + e; }
  }
}


// Show the photo slideshow options only when the screensaver style is photos.
function screensaverModeChanged() {
  const adv = document.getElementById('photo-advanced');
  const mode = document.getElementById('screensaver_mode')?.value;
  if (adv) adv.classList.toggle('d-none', mode !== 'photos');
}

// -- photo sources for the slideshow (FoodAssistant-af1l) --------------------

// The photo-source fields the two screensaver save buttons include in their
// /save bodies. The API key goes through secretVal so a blank field keeps the
// stored key and the trash button erases it, like every other secret.
function photoSourceFields() {
  if (!document.getElementById('photo_source')) return {};
  return {
    photo_source: document.getElementById('photo_source')?.value || 'built-in',
    photo_folder: document.getElementById('photo_folder')?.value?.trim() || '',
    immich_base_url: document.getElementById('immich_base_url')?.value?.trim() || '',
    immich_api_key: secretVal('immich_api_key'),
    immich_album_id: document.getElementById('immich_album_id')?.value?.trim() || '',
    photo_urls: document.getElementById('photo_urls')?.value || '',
  };
}

// Show only the fields the picked photo source needs.
function photoSourceChanged() {
  const source = document.getElementById('photo_source')?.value || 'built-in';
  const groups = {folder: 'photo-source-folder', immich: 'photo-source-immich', urls: 'photo-source-urls'};
  for (const [key, id] of Object.entries(groups)) {
    document.getElementById(id)?.classList.toggle('d-none', source !== key);
  }
}

// Try the photo-source values currently in the form without saving: shows how
// many photos the source would play, and a small preview when one is possible.
async function testPhotoSource() {
  const el = document.getElementById('photo-source-test-result');
  const thumb = document.getElementById('photo-source-test-thumb');
  if (thumb) thumb.innerHTML = '';
  if (el) { el.className = 'test-result text-secondary'; el.textContent = 'Checking...'; }
  const body = photoSourceFields();
  // secretVal returns '' for an untouched field; the test endpoint then uses
  // the stored key, so testing a saved Immich setup just works.
  if (body.immich_api_key === '__CLEAR__') body.immich_api_key = '';
  try {
    const r = await fetch('ui/screensaver/photos/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error('HTTP ' + r.status);
    if (data.ok) {
      if (el) {
        el.className = 'test-result text-success';
        el.textContent = data.count + (data.count === 1 ? ' photo found.' : ' photos found.');
      }
      if (thumb && data.sample) {
        const img = document.createElement('img');
        img.src = data.sample;
        img.alt = 'Sample photo';
        img.style.cssText = 'max-width:160px;max-height:110px;border-radius:6px;object-fit:cover;';
        thumb.appendChild(img);
      }
    } else if (el) {
      el.className = 'test-result text-warning';
      el.textContent = data.detail || 'No photos found.';
    }
  } catch (e) {
    if (el) { el.className = 'test-result text-danger'; el.textContent = 'Test failed: ' + e; }
  }
}
