// On the Line page (templates/current-recipe.html).
const MEALIE_URL = JSON.parse(document.getElementById('current-recipe-data').textContent).mealie_url;
let crRecipe = null;
let crTimerPoll = null;


function fmtQty(q) {
  if (q === null || q === undefined || q === '') return '';
  const n = Number(q);
  if (Number.isNaN(n)) return PR.esc(q);
  // Trim trailing zeros (e.g. 6.0 -> 6, 1.50 -> 1.5).
  return n.toFixed(2).replace(/\.?0+$/, '');
}

function fmtClock(secs) {
  secs = Math.max(0, Math.round(secs));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}:${String(m % 60).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  }
  return `${m}:${String(s).padStart(2,'0')}`;
}

function show(id) { document.getElementById(id).classList.remove('d-none'); }
function hide(id) { document.getElementById(id).classList.add('d-none'); }

let crAll = [];       // every recipe in progress
let crFocus = 0;      // slot of the recipe shown in detail

async function loadCurrentRecipe() {
  // A pending Cooked confirmation belongs to the recipe it was opened for.
  hide('cr-cooked-confirm');
  show('cr-loading');
  hide('cr-empty');
  hide('cr-active');
  hide('cr-courses');
  try {
    const r = await fetch('current-recipe/all');
    if (!r.ok) throw new Error(r.statusText);
    crAll = (await r.json()).recipes || [];
    hide('cr-loading');
    if (!crAll.length) { show('cr-empty'); return; }
    // Keep the focused slot if it still exists, else focus the first recipe.
    if (!crAll.some(x => x.slot === crFocus)) crFocus = crAll[0].slot;
    crRecipe = crAll.find(x => x.slot === crFocus) || crAll[0];
    renderCourseStrip();
    renderRecipe();
    show('cr-active');
    loadEquipment();
    // Timers come from the primary (slot 0); show them only when it is focused.
    if (crFocus === 0) { await loadTimerSuggestions(); startTimerPolling(); }
    else { document.getElementById('cr-timer-suggestions').innerHTML =
      '<span class="small text-secondary">Open the primary recipe for its timers.</span>'; }
  } catch (e) {
    hide('cr-loading');
    show('cr-empty');
    document.getElementById('cr-empty').querySelector('p').innerHTML =
      `<span class="text-danger">Could not load the active recipe: ${PR.esc(e.message)}</span>`;
  }
}

async function loadEquipment() {
  const wrap = document.getElementById('cr-equipment-wrap');
  const list = document.getElementById('cr-equipment');
  const miss = document.getElementById('cr-equipment-missing');
  wrap.classList.add('d-none');
  let data = { equipment: [], missing_appliances: [] };
  try {
    const r = await fetch('current-recipe/equipment?slot=' + crFocus);
    if (r.ok) data = await r.json();
  } catch (e) { return; }
  const eq = data.equipment || [];
  if (!eq.length) return;
  const missing = new Set(data.missing_appliances || []);
  list.innerHTML = '';
  eq.forEach(item => {
    const chip = document.createElement('span');
    const lacking = missing.has(item.name);
    chip.className = 'badge ' + (lacking ? 'text-bg-warning' : 'text-bg-secondary');
    chip.textContent = item.name + (lacking ? ' ?' : '');
    list.appendChild(chip);
  });
  if (missing.size) {
    miss.textContent = 'You may not have: ' + [...missing].join(', ') +
      '. Update your kitchen in Settings > Kitchen > Recipe suggestions.';
    miss.classList.remove('d-none');
  } else { miss.classList.add('d-none'); }
  wrap.classList.remove('d-none');
}

function renderCourseStrip() {
  const strip = document.getElementById('cr-courses');
  if (crAll.length < 2) { strip.classList.add('d-none'); strip.innerHTML = ''; return; }
  strip.classList.remove('d-none');
  strip.innerHTML = '';
  crAll.forEach(rc => {
    const b = document.createElement('button');
    const on = rc.slot === crFocus;
    b.className = 'btn btn-sm ' + (on ? 'btn-primary' : 'btn-outline-primary');
    b.innerHTML = (rc.slot === 0 ? '<i class="bi bi-star-fill me-1"></i>' : '') + PR.esc(rc.title || 'Recipe');
    b.onclick = () => { crFocus = rc.slot; loadCurrentRecipe(); };
    strip.appendChild(b);
  });
}

async function clearCurrentRecipe(btn) {
  const title = (crRecipe && crRecipe.title) ? crRecipe.title : 'this recipe';
  if (!confirm('Stop cooking ' + title + '? Its timers keep running.')) return;
  if (btn) btn.disabled = true;
  try {
    await fetch('current-recipe/' + crFocus, { method: 'DELETE' });
  } catch (e) { /* ignore: reload reflects the truth */ }
  if (btn) btn.disabled = false;
  loadCurrentRecipe();
}

// Cooked is two steps (FoodAssistant-hyr67): the button only opens the
// confirmation, and nothing leaves the pantry until "Use them up" is pressed.
function askCooked() {
  const box = document.getElementById('cr-cooked-confirm');
  box.classList.remove('d-none');
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  document.getElementById('cr-cooked-no').focus();
}

function cancelCooked() {
  document.getElementById('cr-cooked-confirm').classList.add('d-none');
}

async function confirmCooked(btn) {
  if (btn) btn.disabled = true;
  cancelCooked();
  try {
    const r = await fetch('current-recipe/' + crFocus + '/cooked', { method: 'POST' });
    if (!r.ok) throw new Error(await prErrText(r));
    const data = await r.json();
    const n = (data.consumed || []).length;
    // The leftovers prompt now lives in the Pending page Action Items inbox.
    alert('Marked cooked. Consumed ' + n + ' item' + (n === 1 ? '' : 's') +
          ' from inventory. A "save to leftovers?" item is waiting on the Pending page.');
  } catch (e) {
    alert('Could not mark the recipe cooked. ' + prReason(e));
  }
  if (btn) btn.disabled = false;
  loadCurrentRecipe();
}

function renderRecipe() {
  const r = crRecipe;
  document.getElementById('cr-title').textContent = r.title || 'Untitled recipe';
  // Source, plus a "made before" note once this recipe has been cooked
  // (FoodAssistant-bjps). No count shows for a recipe new to you.
  let meta = r.source ? `Source: ${r.source}` : '';
  const madeN = r.cook_count || 0;
  if (madeN) {
    const made = madeN === 1 ? 'Made once before' : `Made ${madeN} times before`;
    meta = meta ? `${meta} · ${made}` : made;
  }
  document.getElementById('cr-source').textContent = meta;

  // Link back to Mealie when this recipe came from there (FoodAssistant-f546).
  const openMealie = document.getElementById('cr-open-mealie');
  const slug = r.id || r.slug;
  if (MEALIE_URL && r.source === 'mealie' && slug) {
    openMealie.href = `${MEALIE_URL}/g/home/r/${encodeURIComponent(slug)}`;
    openMealie.classList.remove('d-none');
  } else {
    openMealie.classList.add('d-none');
  }

  // Servings input reflects scaled servings (base servings * scale).
  const scale = r.servings_scale || 1;
  const base = r.servings || 1;
  document.getElementById('cr-servings').value = Math.round(base * scale);

  // A heading row opens each ingredient group when the recipe has them (sz4h);
  // a recipe with no sections renders as one flat list, exactly as before.
  const ing = document.getElementById('cr-ingredients');
  let prevSection = null;
  ing.innerHTML = (r.ingredients || []).map(i => {
    const qty = i.scaled_quantity != null ? i.scaled_quantity : i.quantity;
    const amount = [fmtQty(qty), PR.esc(i.unit || '')].filter(Boolean).join(' ');
    const section = String(i.section || '').trim();
    let heading = '';
    if (section && section !== prevSection) {
      heading = `<li class="list-group-item bg-transparent px-0 border-0 pb-0 fw-semibold">${PR.esc(section)}</li>`;
    }
    prevSection = section || null;
    return heading + `<li class="list-group-item bg-transparent px-0 d-flex justify-content-between gap-2">
      <span>${PR.esc(i.name)}</span>
      <span class="text-secondary text-nowrap">${amount}</span>
    </li>`;
  }).join('') || '<li class="list-group-item bg-transparent px-0 text-secondary">No ingredients listed.</li>';

  const steps = document.getElementById('cr-steps');
  steps.innerHTML = (r.steps || []).map(s => `<li class="mb-2">${PR.esc(s)}</li>`).join('')
    || '<li class="text-secondary">No steps listed.</li>';

  const notes = document.getElementById('cr-notes');
  if (r.notes) {
    notes.innerHTML = `<i class="bi bi-sticky me-1"></i>${PR.esc(r.notes)}`;
    notes.classList.remove('d-none');
  } else {
    notes.classList.add('d-none');
  }
}

function showToast(msg, isError = false) {
  const toast = document.getElementById('crToast');
  toast.className = `toast align-items-center border-0 text-bg-${isError ? 'danger' : 'success'}`;
  document.getElementById('crToastMsg').textContent = msg;
  new bootstrap.Toast(toast, { delay: 3000 }).show();
}

// Ask before printing the same recipe again within a minute (FoodAssistant-r1nf).
// It is easy to hit Print twice in a row (a stuck job, a double tap); this only
// guards accidental repeats, so it remembers just the last id + time in memory.
const RECENT_PRINT_WINDOW_MS = 60000;
let _lastPrinted = null; // {id, at}
function confirmIfRecentlyPrinted(id) {
  const now = Date.now();
  if (_lastPrinted && _lastPrinted.id === id && (now - _lastPrinted.at) < RECENT_PRINT_WINDOW_MS) {
    if (!confirm('You printed this less than a minute ago. Print it again?')) return false;
  }
  _lastPrinted = { id, at: now };
  return true;
}

// Print the recipe as it appears on screen (FoodAssistant-fb8x). Built as text
// client-side so the printout matches the current scaled servings and works for
// recipes that did not come from Mealie. The server lays it out onto a page.
async function printRecipe(btn) {
  const r = crRecipe;
  if (!r) return;
  const recipeId = r.id || r.slug || r.title;
  if (recipeId && !confirmIfRecentlyPrinted(recipeId)) return;
  const scale = r.servings_scale || 1;
  const base = r.servings || 1;
  const lines = [];
  lines.push(`Serves ${Math.round(base * scale)}`);
  lines.push('');
  lines.push('Ingredients');
  (r.ingredients || []).forEach(i => {
    const qty = i.scaled_quantity != null ? i.scaled_quantity : i.quantity;
    const amount = [fmtQty(qty), i.unit || ''].filter(Boolean).join(' ');
    lines.push(`- ${[amount, i.name].filter(Boolean).join(' ')}`);
  });
  lines.push('');
  lines.push('Steps');
  (r.steps || []).forEach((s, idx) => lines.push(`${idx + 1}. ${s}`));
  if (r.notes) { lines.push(''); lines.push('Notes'); lines.push(r.notes); }
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
  try {
    const resp = await fetch('printing/document', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ title: r.title || 'Recipe', text: lines.join('\n') }),
    });
    const d = await resp.json().catch(() => ({}));
    if (!resp.ok || !d.ok) throw new Error(d.detail || d.error || resp.statusText);
    showToast('Recipe sent to the printer.');
  } catch (e) {
    showToast('Print failed: ' + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function applyScale(factor) {
  try {
    const r = await fetch('current-recipe/' + crFocus + '/scale', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({factor}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    crRecipe = (await r.json()).recipe;
    renderRecipe();
  } catch (e) {
    alert('Could not rescale: ' + e.message);
  }
}

function servingsToFactor(target) {
  const base = crRecipe.servings || 1;
  return Math.max(1, target) / base;
}

function bumpServings(delta) {
  const input = document.getElementById('cr-servings');
  const target = Math.max(1, (parseInt(input.value, 10) || 1) + delta);
  input.value = target;
  applyScale(servingsToFactor(target));
}

document.getElementById('cr-servings')?.addEventListener('change', function() {
  const target = Math.max(1, parseInt(this.value, 10) || 1);
  this.value = target;
  applyScale(servingsToFactor(target));
});

async function loadTimerSuggestions() {
  const box = document.getElementById('cr-timer-suggestions');
  try {
    const r = await fetch('current-recipe/timer-suggestions');
    if (!r.ok) throw new Error(r.statusText);
    const sug = (await r.json()).suggestions || [];
    if (!sug.length) {
      box.innerHTML = '<span class="small text-secondary">No timer suggestions for this recipe.</span>';
      return;
    }
    box.innerHTML = '<div class="d-flex flex-wrap gap-2">' + sug.map(s => `
      <button class="btn btn-sm btn-outline-warning" data-on-click="startSuggested(${s.step_index}, this)"
              title="Start a ${fmtClock(s.seconds)} timer">
        <i class="bi bi-play-fill me-1"></i>${PR.esc(s.label)} (${fmtClock(s.seconds)})
      </button>`).join('') + '</div>';
  } catch (e) {
    box.innerHTML = `<span class="small text-danger">Could not load timer suggestions: ${PR.esc(e.message)}</span>`;
  }
}

async function startSuggested(stepIndex, btn) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  try {
    const r = await fetch('current-recipe/timers/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({step_index: stepIndex}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    await refreshTimers();
  } catch (e) {
    alert('Could not start timer: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function cancelTimer(id) {
  try {
    await fetch('timers/' + id, {method: 'DELETE'});
    await refreshTimers();
  } catch (_) {}
}

let runningTimers = [];

async function refreshTimers() {
  try {
    const r = await fetch('timers');
    if (!r.ok) throw new Error(r.statusText);
    runningTimers = (await r.json()).timers || [];
  } catch (_) {
    runningTimers = [];
  }
  renderTimers();
}

function renderTimers() {
  const box = document.getElementById('cr-running-timers');
  if (!runningTimers.length) {
    box.innerHTML = '<span class="small text-secondary">No timers running.</span>';
    return;
  }
  const nowEpoch = Date.now() / 1000;
  box.innerHTML = '<div class="vstack gap-2">' + runningTimers.map(t => {
    // Tick locally off the shared epoch deadline so the countdown stays smooth
    // between polls.
    const remaining = Math.max(0, t.deadline_epoch - nowEpoch);
    const expired = remaining <= 0;
    return `<div class="d-flex align-items-center justify-content-between gap-2 border rounded px-2 py-1">
      <span class="text-truncate">${PR.esc(t.label)}</span>
      <span class="d-flex align-items-center gap-2">
        <span class="badge ${expired ? 'text-bg-success' : 'text-bg-warning'} font-monospace"
              data-deadline="${t.deadline_epoch}">${expired ? 'Done' : fmtClock(remaining)}</span>
        <button class="btn btn-sm btn-outline-secondary" data-on-click="cancelTimer(${t.id})" title="Cancel timer">
          <i class="bi bi-x-lg"></i>
        </button>
      </span>
    </div>`;
  }).join('') + '</div>';
}

function tickTimers() {
  // Local per-second tick using each badge's shared epoch deadline, so we do not
  // hammer the server every second.
  const nowEpoch = Date.now() / 1000;
  document.querySelectorAll('#cr-running-timers [data-deadline]').forEach(el => {
    const remaining = Number(el.dataset.deadline) - nowEpoch;
    if (remaining <= 0) {
      el.textContent = 'Done';
      el.className = 'badge text-bg-success font-monospace';
    } else {
      el.textContent = fmtClock(remaining);
    }
  });
}

function startTimerPolling() {
  if (crTimerPoll) return;
  refreshTimers();
  // Re-sync with the server every 10s; tick locally each second in between.
  let ticks = 0;
  crTimerPoll = setInterval(() => {
    ticks++;
    if (ticks % 10 === 0) {
      refreshTimers();
    } else {
      tickTimers();
    }
  }, 1000);
}

loadCurrentRecipe();
