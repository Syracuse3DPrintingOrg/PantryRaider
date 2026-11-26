// On the Line page (templates/current-recipe.html).
// The public demo's Mealie is not reachable from a visitor's browser, so it
// offers no Open Mealie links.
const MEALIE_URL = document.documentElement.dataset.demo === '1' ? null : JSON.parse(document.getElementById('current-recipe-data').textContent).mealie_url;
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
    else {
      crSuggestions = [];
      document.getElementById('cr-quick-timers').classList.add('d-none');
    }
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
  if (!(await PR.confirmBox('Its timers keep running.', { title: 'Stop cooking ' + title + '?', ok: 'Stop cooking' }))) return;
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
    PR.notice('Consumed ' + n + ' item' + (n === 1 ? '' : 's') +
          ' from inventory. A "save to leftovers?" item is waiting on the Pending page.',
          { title: 'Marked cooked' });
  } catch (e) {
    PR.notice('Could not mark the recipe cooked. ' + prReason(e));
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

  // Each step has a slot for its own timer buttons, filled once the
  // suggestions are in. On the primary recipe a tap marks the step you are on.
  const steps = document.getElementById('cr-steps');
  const mark = crFocus === 0;
  steps.classList.toggle('can-mark', mark);
  document.getElementById('cr-step-hint').classList.toggle('d-none', !mark || !(r.steps || []).length);
  steps.innerHTML = (r.steps || []).map((s, i) => `<li class="mb-2" data-step="${i + 1}"${mark ? ` data-on-click="markStep(${i + 1})"` : ''}>${PR.esc(s)}<div class="cr-step-timers d-none" data-step-timers="${i}"></div></li>`).join('')
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
async function confirmIfRecentlyPrinted(id) {
  const now = Date.now();
  if (_lastPrinted && _lastPrinted.id === id && (now - _lastPrinted.at) < RECENT_PRINT_WINDOW_MS) {
    if (!(await PR.confirmBox('You printed this less than a minute ago.', { title: 'Print it again?', ok: 'Print' }))) return false;
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
  if (recipeId && !(await confirmIfRecentlyPrinted(recipeId))) return;
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
    PR.notice('Could not rescale: ' + e.message);
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

let crSuggestions = [];
let crStep = null;          // the step the kitchen is on, from 1, or null
const crFinished = new Set();

function suggestionKey(s) { return String(s.label).toLowerCase() + '|' + Math.round(s.seconds); }

// A timer's length for a button: "30 sec", "1 min 30 sec", "10 min",
// "1 hr 30 min". Seconds show under ten minutes, where they matter.
function fmtLength(secs) {
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return `${secs} sec`;
  let h = Math.floor(secs / 3600);
  if (h) {
    let m = Math.round((secs - h * 3600) / 60);
    if (m === 60) { h += 1; m = 0; }
    return m ? `${h} hr ${m} min` : `${h} hr`;
  }
  if (secs >= 600) return `${Math.round(secs / 60)} min`;
  const m = Math.floor(secs / 60), s = secs % 60;
  return s ? `${m} min ${s} sec` : `${m} min`;
}

// The timer a suggestion started: same name and length, as the server
// matches it. A running one wins over a finished one.
function timerForSuggestion(s) {
  const want = suggestionKey(s);
  const now = Date.now() / 1000;
  let ringing = null;
  for (const t of runningTimers) {
    if (String(t.label).toLowerCase() + '|' + Math.round(t.total_seconds) !== want) continue;
    if (t.deadline_epoch - now > 0) return t;
    ringing = t;
  }
  return ringing;
}

function timerButtonHtml(i, withStep) {
  return `<button class="btn cr-tbtn" data-tbtn="${i}" data-with-step="${withStep ? 1 : 0}"
    data-on-click="event.stopPropagation(); startSuggested(${i}, this)"></button>`;
}

// Paint every timer button from the timer list: idle ones say what they
// start, a running one is its own countdown, a finished one says so.
function paintTimerButtons() {
  const now = Date.now() / 1000;
  document.querySelectorAll('[data-tbtn]').forEach(btn => {
    const s = crSuggestions[Number(btn.dataset.tbtn)];
    if (!s) return;
    const t = timerForSuggestion(s);
    const key = suggestionKey(s);
    let state = 'idle', main = `\u25B6 Start ${fmtLength(s.seconds)} \u00B7 ${s.label}`, sub = '';
    if (t && t.deadline_epoch - now <= 0) {
      state = 'ringing'; main = `${s.label} \u00B7 Done`; sub = 'Time is up. Tap to stop the alarm';
      crFinished.add(key);
    } else if (t) {
      state = 'running'; main = `${s.label} \u00B7 ${fmtClock(t.deadline_epoch - now)} left`; sub = 'Running';
    } else if (crFinished.has(key)) {
      state = 'done'; sub = 'Done. Tap to start it again';
    } else if (btn.dataset.withStep === '1') {
      sub = `Step ${s.step_index + 1}`;
    }
    btn.className = 'btn cr-tbtn' + (state === 'idle' ? '' : ' is-' + state);
    const html = PR.esc(main) + (sub ? `<span class="cr-tbtn-sub">${PR.esc(sub)}</span>` : '');
    if (btn.innerHTML !== html) btn.innerHTML = html;
    btn.setAttribute('aria-label', state === 'idle' || state === 'done'
      ? `Start a ${fmtLength(s.seconds)} timer: ${s.label}` : main);
  });
}

async function loadTimerSuggestions() {
  const wrap = document.getElementById('cr-quick-timers');
  const box = document.getElementById('cr-timer-suggestions');
  try {
    const r = await fetch('current-recipe/timer-suggestions');
    if (!r.ok) throw new Error(r.statusText);
    crSuggestions = (await r.json()).suggestions || [];
  } catch (e) {
    crSuggestions = [];
  }
  document.getElementById('cr-quick-timers-label').textContent =
    crSuggestions.length === 1 ? 'Timer in this recipe' : 'Timers in this recipe';
  box.innerHTML = crSuggestions.map((s, i) => timerButtonHtml(i, true)).join('');
  wrap.classList.toggle('d-none', !crSuggestions.length);
  document.querySelectorAll('[data-step-timers]').forEach(slot => {
    const idx = Number(slot.dataset.stepTimers);
    const mine = crSuggestions.map((s, i) => [s, i]).filter(([s]) => s.step_index === idx);
    slot.innerHTML = mine.map(([, i]) => timerButtonHtml(i, false)).join('');
    slot.classList.toggle('d-none', !mine.length);
  });
  paintTimerButtons();
  loadStep();
}

async function startSuggested(i, btn) {
  const s = crSuggestions[i];
  if (!s) return;
  const t = timerForSuggestion(s);
  if (t && t.deadline_epoch - Date.now() / 1000 <= 0) {
    try { await fetch('timers/' + t.id + '/dismiss', { method: 'POST' }); } catch (_) {}
    await refreshTimers();
    return;
  }
  if (t) { PR.notice(`${s.label} is already running.`); return; }
  btn.disabled = true;
  try {
    const r = await fetch('current-recipe/timers/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({step_index: s.step_index, label: s.label, seconds: s.seconds}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    crFinished.delete(suggestionKey(s));
    await refreshTimers();
  } catch (e) {
    PR.notice('Could not start timer: ' + e.message);
  } finally {
    btn.disabled = false;
  }
}

// The step the kitchen is on lives on the server, so a tap here and "next
// step" said to a voice assistant move the same marker.
function paintSteps() {
  document.querySelectorAll('#cr-steps > li[data-step]').forEach(li => {
    const n = Number(li.dataset.step);
    li.classList.toggle('is-on', crStep === n);
    li.classList.toggle('is-done', crStep !== null && n < crStep);
  });
}

async function loadStep() {
  if (crFocus !== 0) return;
  try {
    const r = await fetch('api/screens/recipe');
    if (!r.ok) return;
    const d = await r.json();
    if (d.ok && d.title === (crRecipe && crRecipe.title)) { crStep = d.step || null; paintSteps(); }
  } catch (_) { /* keep what is shown */ }
}

async function markStep(n) {
  try {
    const r = await fetch('api/screens/recipe/step', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      // adopt: on a tablet showing the whole interface, the kitchen's recipe
      // becomes that screen's own, so voice there follows the same steps.
      body: JSON.stringify({step: crStep === n ? 'none' : n, adopt: true}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    crStep = (await r.json()).step || null;
    paintSteps();
  } catch (e) {
    PR.notice('Could not mark the step. ' + prReason(e));
  }
}

async function cancelTimer(id) {
  try {
    await fetch('timers/' + id, {method: 'DELETE'});
    await refreshTimers();
  } catch (_) {}
}

let runningTimers = [];

async function refreshTimers(signal) {
  try {
    const r = await fetch('timers', { signal });
    if (!r.ok) throw new Error(r.statusText);
    runningTimers = (await r.json()).timers || [];
  } catch (_) {
    // A timed refresh that ran out of time keeps the timers it had.
    if (signal && signal.aborted) return;
    runningTimers = [];
  }
  renderTimers();
  paintTimerButtons();
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
  paintTimerButtons();
}

function startTimerPolling() {
  if (crTimerPoll) return;
  refreshTimers();
  // Re-sync with the server every 10s; tick locally each second in between.
  // The re-sync goes through PR.poller: a slow answer never stacks a second
  // request, and the request is dropped when the page is left.
  const resync = PR.poller(refreshTimers);
  let ticks = 0;
  crTimerPoll = setInterval(() => {
    ticks++;
    if (ticks % 10 === 0) {
      resync();
      loadStep();
    } else {
      tickTimers();
    }
  }, 1000);
}

// Cook mode over this page. ?cook=1 opens it (a voice request sends the
// kitchen display here when someone says "let's cook ...").
let crCook = null;
function openCookMode() {
  const root = document.getElementById('cr-cook');
  root.removeAttribute('hidden');
  document.body.classList.add('cr-cooking');
  if (!crCook) {
    crCook = PRCook.mount(root, { screen: '', ownTimers: true, exit: closeCookMode });
  } else {
    crCook.refresh();
  }
}
function closeCookMode() {
  document.getElementById('cr-cook').setAttribute('hidden', '');
  document.body.classList.remove('cr-cooking');
  if (new URLSearchParams(location.search).has('cook')) {
    history.replaceState(null, '', location.pathname);
  }
  loadCurrentRecipe();
}

loadCurrentRecipe();
if (new URLSearchParams(location.search).get('cook') === '1') openCookMode();
