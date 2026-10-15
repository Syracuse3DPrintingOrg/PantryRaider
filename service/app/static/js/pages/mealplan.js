// Meal Plan page (templates/mealplan.html).
// MEALIE_URL is null when the plan lives in Pantry Raider's own store; recipe
// titles then render as plain text instead of Mealie deep links.
// The public demo's Mealie is not reachable from a visitor's browser, so it
// offers no Open Mealie links.
const MEALIE_URL = document.documentElement.dataset.demo === '1' ? null : JSON.parse(document.getElementById('mealplan-data').textContent).mealie_url;
const TYPE_ORDER = {breakfast: 0, lunch: 1, dinner: 2, side: 3};
const TYPE_BADGE = {breakfast: 'info', lunch: 'success', dinner: 'primary', side: 'secondary'};



function planEntryRow(e) {
  return `
        <div class="d-flex align-items-center justify-content-between py-1">
          <div>
            <span class="badge bg-${TYPE_BADGE[e.entry_type] || 'secondary'} me-2">${PR.esc(e.entry_type)}</span>
            ${e.recipe_slug && MEALIE_URL
              ? `<a href="${PR.esc(MEALIE_URL)}/g/home/r/${PR.esc(encodeURIComponent(e.recipe_slug))}" target="_blank">${PR.esc(e.title)}</a>`
              : PR.esc(e.title)}
          </div>
          <button class="btn btn-sm btn-outline-danger btn-icon" data-on-click="removeEntry(${PR.escAttrJs(e.id)})" title="Remove">
            <i class="bi bi-x-lg"></i>
          </button>
        </div>`;
}

function recipeResultsHtml(items) {
  return items.slice(0, 6).map(it =>
    `<button type="button" class="list-group-item list-group-item-action py-1"
        data-on-click="pickRecipe(${PR.escAttrJs(it.id)}, this.textContent)">${PR.esc(it.name)}</button>`).join('');
}

function dayLabel(iso) {
  const d = new Date(iso + 'T00:00:00');
  const today = new Date(); today.setHours(0,0,0,0);
  const diff = Math.round((d - today) / 86400000);
  const name = d.toLocaleDateString(undefined, {weekday: 'long', month: 'short', day: 'numeric'});
  if (diff === 0) return 'Today: ' + name;
  if (diff === 1) return 'Tomorrow: ' + name;
  return name;
}

async function loadPlan() {
  const el = document.getElementById('plan');
  try {
    const r = await fetch('mealie/mealplan?days=7');
    if (!r.ok) throw new Error(await prErrText(r));
    const data = await r.json();
    el.innerHTML = '';
    for (const [day, entries] of Object.entries(data.days)) {
      entries.sort((a, b) => (TYPE_ORDER[a.entry_type] ?? 9) - (TYPE_ORDER[b.entry_type] ?? 9));
      const card = document.createElement('div');
      card.className = 'card';
      const rows = entries.length ? entries.map(planEntryRow).join('')
        : '<div class="text-secondary small py-1">Nothing planned</div>';
      card.innerHTML = `
        <div class="card-body py-2">
          <div class="fw-semibold mb-1">${PR.esc(dayLabel(day))}</div>
          ${rows}
        </div>`;
      el.appendChild(card);
    }
  } catch (e) {
    el.innerHTML = `<div class="alert alert-danger">Could not load meal plan: ${PR.esc(e.message)}</div>`;
  }
}

async function removeEntry(id) {
  await fetch('mealie/mealplan/' + id, {method: 'DELETE'});
  loadPlan();
}


// Recipe search in the add-entry modal (debounced)
let searchTimer = null;
document.getElementById('recipe-search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  document.getElementById('recipe-id').value = '';
  document.getElementById('recipe-picked').textContent = '';
  if (q.length < 2) { document.getElementById('recipe-results').innerHTML = ''; return; }
  searchTimer = setTimeout(async () => {
    const r = await fetch('mealie/recipes?search=' + encodeURIComponent(q));
    const items = r.ok ? await r.json() : [];
    document.getElementById('recipe-results').innerHTML = recipeResultsHtml(items);
  }, 300);
});

function pickRecipe(id, name) {
  document.getElementById('recipe-id').value = id;
  document.getElementById('recipe-picked').textContent = 'Selected: ' + name.trim();
  document.getElementById('recipe-results').innerHTML = '';
}

async function saveEntry() {
  const err = document.getElementById('entry-error');
  err.classList.add('d-none');
  const body = {
    date: document.getElementById('entry-date').value,
    entry_type: document.getElementById('entry-type').value,
    recipe_id: document.getElementById('recipe-id').value || null,
    title: document.getElementById('entry-title').value.trim(),
  };
  if (!body.date) { err.textContent = 'Pick a date.'; err.classList.remove('d-none'); return; }
  const r = await fetch('mealie/mealplan', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!r.ok) {
    err.textContent = await prErrText(r);
    err.classList.remove('d-none');
    return;
  }
  bootstrap.Modal.getInstance(document.getElementById('addEntryModal')).hide();
  document.getElementById('entry-title').value = '';
  document.getElementById('recipe-search').value = '';
  document.getElementById('recipe-id').value = '';
  document.getElementById('recipe-picked').textContent = '';
  loadPlan();
}

document.getElementById('entry-date').value = new Date().toISOString().slice(0, 10);
loadPlan();
