// Your recipe databases (FoodAssistant-1sfs6): the list of recipe catalogs
// under Recipes, plus one-click presets and the add-by-address form. Every
// value shown goes through PR.esc and every value handed to a handler through
// PR.escAttrJs, so nothing a catalog reports can become markup or code.

function catalogResult(html) {
  const el = document.getElementById('catalog-result');
  if (el) el.innerHTML = html;
}

function catalogOk(msg) {
  catalogResult(`<span class="text-success"><i class="bi bi-check-circle me-1"></i>${PR.esc(msg)}</span>`);
}

function catalogFail(msg) {
  catalogResult(`<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${PR.esc(msg)}</span>`);
}

function renderCatalogs(data) {
  const list = document.getElementById('catalog-list');
  const presets = document.getElementById('catalog-presets');
  if (!list) return;
  const rows = data.catalogs || [];
  list.innerHTML = rows.length ? rows.map(c => {
    const idArg = PR.escAttrJs(c.id);
    return `
      <div class="set-row">
        <div class="set-row-label">
          <div class="set-row-name">${PR.esc(c.name)}${c.has_key ? ' <i class="bi bi-key ms-1 text-secondary" title="Uses an access key"></i>' : ''}</div>
          <div class="set-row-hint text-break">${PR.esc(c.url)}</div>
        </div>
        <div class="set-row-actions">
          <label class="set-toggle">
            <input type="checkbox" role="switch" aria-label="Show recipes from ${PR.esc(c.name)}" ${c.enabled ? 'checked' : ''}
                   data-on-change="toggleCatalog(${idArg}, this)">
            <span class="set-toggle-track"><span class="set-toggle-thumb"></span></span>
          </label>
          <button type="button" class="btn btn-outline-danger btn-sm" title="Remove ${PR.esc(c.name)}"
                  data-on-click="removeCatalog(${idArg}, ${PR.escAttrJs(c.name)}, this)">
            <i class="bi bi-trash"></i>
          </button>
        </div>
      </div>`;
  }).join('') : '<div class="form-text mb-1">No recipe databases yet. Add one below.</div>';
  if (presets) {
    presets.innerHTML = (data.presets || []).map(p => `
      <button type="button" class="btn btn-outline-secondary btn-sm me-1 mb-1" title="${PR.esc(p.description || '')}"
              data-on-click="addCatalogPreset(${PR.escAttrJs(p.url)}, ${PR.escAttrJs(p.name)}, this)">
        <i class="bi bi-plus-lg me-1"></i>Add ${PR.esc(p.name)}
      </button>`).join('');
  }
  const pill = document.getElementById('catalogs-pill');
  if (pill) {
    pill.textContent = rows.length ? `${rows.length} added` : 'None yet';
    pill.classList.toggle('set-pill-good', rows.length > 0);
    pill.classList.toggle('set-pill-neutral', rows.length === 0);
  }
}

async function loadCatalogs() {
  if (!document.getElementById('catalog-list')) return;
  try {
    const r = await fetch('setup/recipe-catalogs');
    if (r.ok) renderCatalogs(await r.json());
  } catch (e) { /* the panel just stays empty; Add still works */ }
}

async function catalogRequest(url, method, body) {
  const r = await fetch(url, {
    method, headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let d = {};
  try { d = await r.json(); } catch (e) { /* keep the generic message */ }
  if (!r.ok) throw new Error(d.detail || 'That did not work. Try again.');
  return d;
}

function catalogForm() {
  return {
    url: (document.getElementById('catalog_url') || {}).value || '',
    name: (document.getElementById('catalog_name') || {}).value || '',
    api_key: (document.getElementById('catalog_key') || {}).value || '',
  };
}

async function busy(btn, fn) {
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
  try { return await fn(); } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function testCatalog(btn) {
  const f = catalogForm();
  if (!f.url.trim()) return catalogFail('Enter the recipe database address first.');
  await busy(btn, async () => {
    try {
      const d = await catalogRequest('setup/recipe-catalogs/test', 'POST', {url: f.url, api_key: f.api_key});
      (d.ok ? catalogOk : catalogFail)(d.message);
    } catch (e) { catalogFail(e.message); }
  });
}

async function addCatalog(btn) {
  const f = catalogForm();
  if (!f.url.trim()) return catalogFail('Enter the recipe database address first.');
  await busy(btn, async () => {
    try {
      const d = await catalogRequest('setup/recipe-catalogs', 'POST', f);
      renderCatalogs(d);
      ['catalog_url', 'catalog_name', 'catalog_key'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      catalogOk(d.message);
    } catch (e) { catalogFail(e.message); }
  });
}

async function addCatalogPreset(url, name, btn) {
  await busy(btn, async () => {
    try {
      const d = await catalogRequest('setup/recipe-catalogs', 'POST', {url, name});
      renderCatalogs(d);
      catalogOk(d.message);
    } catch (e) { catalogFail(e.message); }
  });
}

async function toggleCatalog(id, el) {
  try {
    const d = await catalogRequest(`setup/recipe-catalogs/${encodeURIComponent(id)}/enabled`, 'POST', {enabled: el.checked});
    renderCatalogs(d);
  } catch (e) {
    el.checked = !el.checked;
    catalogFail(e.message);
  }
}

async function removeCatalog(id, name, btn) {
  if (!(await PR.confirmBox('Its recipes stop showing up here. Recipes you already saved stay in your library.',
                            {title: `Remove ${name}?`, ok: 'Remove'}))) return;
  await busy(btn, async () => {
    try {
      renderCatalogs(await catalogRequest(`setup/recipe-catalogs/${encodeURIComponent(id)}`, 'DELETE'));
      catalogOk(`${name} removed.`);
    } catch (e) { catalogFail(e.message); }
  });
}

document.addEventListener('DOMContentLoaded', loadCatalogs);
