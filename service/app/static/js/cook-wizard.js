/*
 * Cook wizard: a choose-your-own-adventure front end for the Cook page.
 *
 * This is a small declarative state machine, no framework. It reuses the Cook
 * page's own plumbing rather than duplicating it:
 *   - openCookPreview(id) / cookCards  -> the in-app recipe quick view (Cook,
 *     Add to cart, Print all keep working)
 *   - openAiPreview(name, extra)       -> the AI generate + preview modal
 *   - /mealie/suggest                  -> the stock-matched local + web tiers
 *   - /mealie/recipes                  -> name search across Mealie and the web
 *
 * Every guided step is skippable and the flow stays shallow (cuisine, dish
 * type, diet) so it reads on a 1024x600 kitchen panel. It fails soft: a fetch
 * error shows a message inside the wizard, it never throws through the page.
 */
(function () {
  'use strict';

  // ai_configured is injected by the Cook template (window.__cookWizardAi).
  function aiOn() { return !!window.__cookWizardAi; }

  let opts = null;               // cached /cook-wizard/options payload
  let modal = null;              // bootstrap.Modal instance
  const state = { filters: {} }; // cuisine / category / diet accumulate here
  const history = [];            // step-render functions, for the Back button
  let cwSeq = 0;                 // quick-view card id namespace (own keyspace)

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    // Reuse the page helper when present; otherwise a local fallback.
    if (typeof escHtml === 'function') return escHtml(s);
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Render a step. `fn` draws into #cw-body; we remember it so Back can replay.
  function go(fn, push) {
    if (push !== false) history.push(fn);
    updateBack();
    fn();
  }
  function back() {
    if (history.length <= 1) return;
    history.pop();
    const prev = history[history.length - 1];
    updateBack();
    prev();
  }
  function updateBack() {
    const b = el('cw-back');
    if (b) b.classList.toggle('invisible', history.length <= 1);
  }

  function setTitle(t) { const n = el('cw-title'); if (n) n.textContent = t; }
  function body() { return el('cw-body'); }

  // A grid of big touch buttons. `items` are {value,label,emoji}; `onPick`
  // receives the chosen value. A Skip button is always appended.
  function bigButtonGrid(items, onPick, skipLabel) {
    const wrap = document.createElement('div');
    wrap.className = 'cw-grid';
    items.forEach(function (it) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-outline-primary cw-tile';
      btn.innerHTML = (it.emoji ? '<span class="cw-emoji">' + it.emoji + '</span>' : '') +
        '<span class="cw-tile-label">' + esc(it.label) + '</span>';
      btn.addEventListener('click', function () { onPick(it.value); });
      wrap.appendChild(btn);
    });
    const skip = document.createElement('button');
    skip.type = 'button';
    skip.className = 'btn btn-outline-secondary cw-tile cw-skip';
    skip.innerHTML = '<span class="cw-emoji">⏭️</span><span class="cw-tile-label">' +
      esc(skipLabel || 'Skip') + '</span>';
    skip.addEventListener('click', function () { onPick(null); });
    wrap.appendChild(skip);
    return wrap;
  }

  // ---- Step 1: pick a path -------------------------------------------------
  function stepStart() {
    setTitle('Cook wizard');
    const b = body();
    b.innerHTML = '';

    // "I know what I want" card.
    const know = document.createElement('div');
    know.className = 'cw-panel';
    know.innerHTML =
      '<h5 class="cw-panel-title">🍽️ I know what I want</h5>' +
      '<p class="text-secondary small mb-2">Type a dish and search everywhere, or have AI write it for you.</p>' +
      '<input type="text" class="form-control form-control-lg mb-2" id="cw-know-input" ' +
      'placeholder="e.g. chicken alfredo, tikka masala, banana bread" autocomplete="off">' +
      '<div class="d-flex flex-wrap gap-2">' +
      '<button type="button" class="btn btn-primary btn-lg flex-grow-1" id="cw-search-btn">' +
      '<i class="bi bi-search me-1"></i>Search my recipes and the web</button>' +
      (aiOn() ? '<button type="button" class="btn btn-info btn-lg flex-grow-1" id="cw-ai-btn">' +
        '<i class="bi bi-stars me-1"></i>Create it with AI</button>' : '') +
      '</div>';
    b.appendChild(know);

    // "Help me find a recipe" card.
    const help = document.createElement('div');
    help.className = 'cw-panel mt-3';
    help.innerHTML =
      '<h5 class="cw-panel-title">🧭 Help me find a recipe</h5>' +
      '<p class="text-secondary small mb-2">Answer a couple of quick questions and we will match recipes to your pantry.</p>' +
      '<button type="button" class="btn btn-outline-primary btn-lg w-100" id="cw-guide-btn">' +
      '<i class="bi bi-compass me-1"></i>Start the guided finder</button>';
    b.appendChild(help);

    const input = el('cw-know-input');
    el('cw-search-btn').addEventListener('click', function () {
      const q = (input.value || '').trim();
      if (!q) { input.focus(); return; }
      go(function () { stepSearchResults(q); });
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); el('cw-search-btn').click(); }
    });
    if (aiOn()) {
      el('cw-ai-btn').addEventListener('click', function () {
        const q = (input.value || '').trim();
        if (!q) { input.focus(); return; }
        // Reuse the Cook page AI preview modal. Close the wizard so the two
        // modals do not stack.
        if (modal) modal.hide();
        if (typeof openAiPreview === 'function') openAiPreview(q);
      });
    }
    el('cw-guide-btn').addEventListener('click', function () {
      state.filters = {};
      go(stepCuisine);
    });
  }

  // ---- Guided steps --------------------------------------------------------
  async function ensureOptions() {
    if (opts) return opts;
    const r = await fetch('cook-wizard/options');
    opts = await r.json();
    return opts;
  }

  function guidedShell(title, subtitle) {
    setTitle('Cook wizard');
    const b = body();
    b.innerHTML = '<h5 class="cw-step-title">' + esc(title) + '</h5>' +
      (subtitle ? '<p class="text-secondary small">' + esc(subtitle) + '</p>' : '') +
      '<div class="text-secondary small py-3" id="cw-step-loading">' +
      '<span class="spinner-border spinner-border-sm me-2"></span>Loading options…</div>';
    return b;
  }

  async function stepCuisine() {
    const b = guidedShell('Pick a cuisine or region', 'Step 1 of 3');
    try {
      const o = await ensureOptions();
      const items = (o.cuisines.regions || []).concat(o.cuisines.cuisines || []);
      b.querySelector('#cw-step-loading').remove();
      b.appendChild(bigButtonGrid(items, function (val) {
        if (val) state.filters.cuisine = val; else delete state.filters.cuisine;
        go(stepCategory);
      }, 'Any cuisine'));
    } catch (e) { stepError(b, e); }
  }

  async function stepCategory() {
    const b = guidedShell('What kind of dish?', 'Step 2 of 3');
    try {
      const o = await ensureOptions();
      b.querySelector('#cw-step-loading').remove();
      b.appendChild(bigButtonGrid(o.categories || [], function (val) {
        if (val) state.filters.category = val; else delete state.filters.category;
        go(stepDiet);
      }, 'Any dish'));
    } catch (e) { stepError(b, e); }
  }

  async function stepDiet() {
    const b = guidedShell('Any dietary needs?', 'Step 3 of 3');
    try {
      const o = await ensureOptions();
      b.querySelector('#cw-step-loading').remove();
      b.appendChild(bigButtonGrid(o.diets || [], function (val) {
        if (val) state.filters.diet = val; else delete state.filters.diet;
        go(stepGuidedResults);
      }, 'No restriction'));
    } catch (e) { stepError(b, e); }
  }

  function stepError(b, e) {
    const l = b.querySelector('#cw-step-loading');
    if (l) l.remove();
    const d = document.createElement('div');
    d.className = 'alert alert-danger';
    d.textContent = 'Could not load the wizard options: ' + (e && e.message ? e.message : e);
    b.appendChild(d);
  }

  // A short human summary of the accumulated filters, for the results heading.
  function filterSummary() {
    const f = state.filters;
    const parts = [];
    if (f.cuisine) parts.push(f.cuisine);
    if (f.category) parts.push(f.category);
    if (f.diet) parts.push(f.diet);
    return parts.join(' · ') || 'your pantry';
  }

  // ---- Results: guided (uses /mealie/suggest) ------------------------------
  async function stepGuidedResults() {
    setTitle('Suggested recipes');
    const b = body();
    b.innerHTML = '<div class="text-secondary small mb-2">Matching <strong>' +
      esc(filterSummary()) + '</strong> against your inventory…</div>' +
      '<div class="text-secondary py-3" id="cw-results-loading">' +
      '<span class="spinner-border spinner-border-sm me-2"></span>Finding recipes…</div>' +
      '<div id="cw-results"></div>';
    try {
      const p = new URLSearchParams();
      if (state.filters.cuisine) p.set('cuisine', state.filters.cuisine);
      if (state.filters.diet) p.set('dietary', state.filters.diet);
      const r = await fetch('mealie/suggest?' + p.toString());
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.statusText);
      // Local and web candidates are classified into separate tier sets
      // (server-side, so the Cook page's dedicated web section still shows
      // regardless of how many local recipes match), but the wizard shows
      // one flat list, so merge them and rank by match score instead of
      // always listing local recipes first regardless of relevance
      // (FoodAssistant-nomr).
      const items = flattenTiers(data.tiers).concat(flattenTiers(data.external_tiers))
        .sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
      el('cw-results-loading').remove();
      renderResults(el('cw-results'), items);
    } catch (e) {
      const load = el('cw-results-loading');
      if (load) load.remove();
      showResultsError(el('cw-results'), e);
    }
  }

  function flattenTiers(tiers) {
    if (!tiers) return [];
    const out = [];
    ['ready', 'staples', 'shopping'].forEach(function (k) {
      (tiers[k] || []).forEach(function (s) { out.push(s); });
    });
    return out;
  }

  // ---- Results: search (uses /mealie/recipes) ------------------------------
  async function stepSearchResults(query) {
    setTitle('Search results');
    const b = body();
    b.innerHTML = '<div class="text-secondary small mb-2">Recipes matching <strong>' +
      esc(query) + '</strong> in your library and on the web.</div>' +
      '<div class="text-secondary py-3" id="cw-results-loading">' +
      '<span class="spinner-border spinner-border-sm me-2"></span>Searching…</div>' +
      '<div id="cw-results"></div>';
    try {
      const p = new URLSearchParams({ search: query, mine: 'true', external: 'true' });
      const r = await fetch('mealie/recipes?' + p.toString());
      const data = await r.json();
      if (!r.ok) throw new Error((data && data.detail) || r.statusText);
      el('cw-results-loading').remove();
      renderResults(el('cw-results'), Array.isArray(data) ? data : [], query);
    } catch (e) {
      const load = el('cw-results-loading');
      if (load) load.remove();
      showResultsError(el('cw-results'), e, query);
    }
  }

  function showResultsError(container, e, query) {
    const msg = (e && e.message) ? e.message : e;
    container.innerHTML = '<div class="alert alert-danger">Could not load recipes: ' +
      esc(String(msg)) + '</div>';
    if (aiOn()) container.appendChild(aiFallbackButton(query));
  }

  // Render a large-format tappable list. Each row registers its suggestion
  // object into the shared cookCards map and opens the existing quick view, so
  // Cook / Add to cart / Print all work exactly as on the Cook page.
  function renderResults(container, items, query) {
    container.innerHTML = '';
    if (!items || !items.length) {
      const none = document.createElement('div');
      none.className = 'text-secondary small mb-2';
      none.textContent = 'No recipes found for this. Try skipping a step, or ask AI.';
      container.appendChild(none);
    } else {
      const list = document.createElement('div');
      list.className = 'cw-results-list';
      items.forEach(function (s) {
        const cid = 'cw' + (++cwSeq);
        if (typeof cookCards === 'object') cookCards[cid] = s;
        const have = (s.matched_ingredients || []).length;
        const total = s.total_ingredients || 0;
        const cov = total ? '<span class="badge text-bg-secondary ms-2">' + have + '/' + total + ' in stock</span>' : '';
        const src = (s.source && s.source !== 'mealie')
          ? '<span class="badge text-bg-info ms-2"><i class="bi bi-globe2 me-1"></i>Web</span>'
          : '<span class="badge text-bg-secondary ms-2"><i class="bi bi-journal me-1"></i>My library</span>';
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'btn btn-outline-secondary cw-result-row text-start';
        row.innerHTML =
          (s.image ? '<img src="' + esc(s.image) + '" class="cw-result-img" alt="" ' +
            'onerror="this.remove()">' : '') +
          '<span class="cw-result-text"><span class="cw-result-name">' + esc(s.name) + src + cov + '</span>' +
          (s.description ? '<span class="cw-result-desc">' + esc(s.description) + '</span>' : '') +
          '</span><i class="bi bi-chevron-right cw-result-chev"></i>';
        row.addEventListener('click', function () {
          if (modal) modal.hide();
          if (typeof openCookPreview === 'function') openCookPreview(cid);
        });
        list.appendChild(row);
      });
      container.appendChild(list);
    }
    if (aiOn()) container.appendChild(aiFallbackButton(query));
  }

  // "Ask AI instead" on any results screen. Builds a name from the accumulated
  // guided filters or the search text and hands off to the Cook page AI modal.
  function aiFallbackButton(query) {
    const wrap = document.createElement('div');
    wrap.className = 'mt-3 pt-2 border-top';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-info w-100 btn-lg';
    btn.innerHTML = '<i class="bi bi-stars me-1"></i>Ask AI to invent one instead';
    btn.addEventListener('click', function () {
      let name = (query || '').trim();
      if (!name) {
        const f = state.filters;
        name = [f.cuisine, f.diet, f.category].filter(Boolean).join(' ').trim() || 'a dish';
      }
      if (modal) modal.hide();
      if (typeof openAiPreview === 'function') openAiPreview(name);
    });
    wrap.appendChild(btn);
    return wrap;
  }

  // ---- Launch --------------------------------------------------------------
  function launch() {
    const node = el('cookWizardModal');
    if (!node || typeof bootstrap === 'undefined') return;
    modal = bootstrap.Modal.getOrCreateInstance(node);
    history.length = 0;
    state.filters = {};
    go(stepStart);
    modal.show();
  }

  document.addEventListener('DOMContentLoaded', function () {
    const back = el('cw-back');
    if (back) back.addEventListener('click', backHandler);
  });
  function backHandler() { back(); }

  // Public entry point wired to the Cook page launch button.
  window.launchCookWizard = launch;
})();
