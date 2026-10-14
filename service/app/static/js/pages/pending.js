// Pending scans page (templates/pending.html).
const CATEGORIES = ['Poultry','Meat','Seafood','Dairy','Produce','Grains','Condiments','Beverages','Snacks','Frozen','Canned','Other'];
const STORAGES = {refrigerated:'Refrigerated', frozen:'Frozen', room_temp:'Room Temp', dry:'Dry'};


function options(list, selected) {
  if (Array.isArray(list)) {
    return list.map(v => `<option value="${v}" ${v===selected?'selected':''}>${v}</option>`).join('');
  }
  return Object.entries(list).map(([v,label]) =>
    `<option value="${v}" ${v===selected?'selected':''}>${label}</option>`).join('');
}

function pendingNotice(text) {
  const el = document.getElementById('pending-notice');
  if (!el) return;
  if (!text) { el.classList.add('d-none'); el.textContent = ''; return; }
  el.textContent = text;
  el.classList.remove('d-none');
}

async function loadPending() {
  let r, data;
  try {
    r = await fetch('pending/');
    // A plain-text 500 or an HTML error page used to reject here, before the
    // status check, so the page showed no list, no empty state and nothing to
    // read, and the refresh repeated that in silence every 15 seconds.
    data = await r.json().catch(() => ({}));
  } catch (e) {
    // Leave whatever is already on screen: a dropped connection during the
    // background refresh is no reason to lose the rows you were working on.
    pendingNotice('The pending list did not refresh. ' + prReason(e));
    return;
  }
  pendingNotice('');
  if (!r.ok) {
    // A satellite whose main server is unreachable gets an honest JSON
    // detail; show that instead of pretending the queue is empty
    // (FoodAssistant-2cmm).
    document.getElementById('pending-empty').classList.add('d-none');
    document.getElementById('pending-list').innerHTML =
      `<div class="alert alert-warning"><i class="bi bi-plug me-2"></i>${PR.esc(data.detail || 'The pending list did not load.')}</div>`;
    document.getElementById('commitAllBtn').disabled = true;
    return;
  }
  const items = data.items || [];
  document.getElementById('pending-count').textContent = items.length;
  document.getElementById('pending-empty').classList.toggle('d-none', items.length > 0);
  document.getElementById('commitAllBtn').disabled = items.length === 0;

  document.getElementById('pending-list').innerHTML = items.map(item => `
    <div class="card mb-2 pending-card ${item.lookup_failed ? 'failed' : ''}" id="pending-${item.id}">
      ${item.store_local ? `
      <div class="alert alert-warning mb-0 rounded-bottom-0 py-2 px-3">
        <i class="bi bi-camera me-1"></i>This barcode looks like a store label (like a deli or scale
        sticker), so it can't be looked up. Discard this entry and
        <a href="ui/add">take a photo of the item instead</a> for an accurate match.
      </div>` : ''}
      <div class="card-body py-2">
        <div class="row g-2 align-items-center">
          <div class="col-md-3">
            <input class="form-control form-control-sm ${item.lookup_failed ? 'border-warning' : ''}"
                   value="${PR.esc(item.name)}" placeholder="Name"
                   data-on-change="patchItem(${item.id}, 'name', this.value, this)">
          </div>
          <div class="col-4 col-md-1">
            <input class="form-control form-control-sm" type="number" value="${item.quantity}"
                   min="0.01" step="0.01"
                   data-on-change="patchItem(${item.id}, 'quantity', parseFloat(this.value), this)">
          </div>
          <div class="col-4 col-md-1">
            <input class="form-control form-control-sm" value="${PR.esc(item.unit || 'item')}"
                   data-on-change="patchItem(${item.id}, 'unit', this.value, this)">
          </div>
          <div class="col-4 col-md-2">
            <select class="form-select form-select-sm" data-on-change="patchItem(${item.id}, 'category', this.value, this)">
              ${options(CATEGORIES, item.category)}
            </select>
          </div>
          <div class="col-6 col-md-2">
            <select class="form-select form-select-sm" data-on-change="patchItem(${item.id}, 'storage_type', this.value, this)">
              ${options(STORAGES, item.storage_type)}
            </select>
          </div>
          <div class="col-6 col-md-2">
            <input class="form-control form-control-sm" type="date" value="${item.best_by_date || ''}"
                   id="pending-date-${item.id}"
                   data-on-change="patchItem(${item.id}, 'best_by_date', this.value, this)">
          </div>
          <div class="col-md-1 text-end d-flex gap-1 justify-content-end">
            <button class="btn btn-sm btn-outline-success btn-icon" title="Commit this item"
                    data-on-click="commitOne(${item.id})">
              <i class="bi bi-cloud-upload"></i>
            </button>
            <button class="btn btn-sm btn-outline-danger btn-icon" title="Discard"
                    data-on-click="discardItem(${item.id})">
              <i class="bi bi-x-lg"></i>
            </button>
          </div>
        </div>
        ${item.duplicate ? `
        <div class="mt-1 ms-1">
          <span class="badge text-bg-info" title="This product already has stock in your inventory. You can still add it; scanned on a different day it lands as a separate entry so each keeps its own expiration.">
            <i class="bi bi-info-circle me-1"></i>Already in inventory (duplicate)
          </span>
        </div>` : ''}
        ${item.sniff ? `
        <div class="mt-1 ms-1 d-flex align-items-center gap-2 flex-wrap">
          <div class="btn-group btn-group-sm" role="group"
               aria-label="Sniff test passed: keep the ${PR.esc(item.name)} already in stock a little longer">
            <span class="btn btn-outline-info pe-none"><i class="bi bi-emoji-smile me-1"></i>Sniff test</span>
            ${[1, 3, 5].map(d => `
            <button type="button" class="btn btn-outline-info fw-semibold px-2"
                    title="Passed the sniff test: keep the ${PR.esc(item.name)} already in stock ${d} more day${d !== 1 ? 's' : ''}"
                    data-on-click="sniffTest(${item.sniff.product_id}, ${PR.esc(JSON.stringify(item.name))}, ${d})">
              +${d}d
            </button>`).join('')}
          </div>
          <span class="meta-text">${item.sniff.days_remaining < 0
            ? `the stock you already have expired ${-item.sniff.days_remaining} day${item.sniff.days_remaining === -1 ? '' : 's'} ago`
            : `the stock you already have is best by ${item.sniff.best_before_date}`}</span>
          <a href="#" class="small text-danger text-decoration-none"
             title="It did not pass: toss the ${PR.esc(item.name)} already in stock (records it as spoiled and removes it)"
             data-on-click="tossStock(${item.sniff.product_id}, ${PR.esc(JSON.stringify(item.name))}); return false;">
            <i class="bi bi-trash me-1"></i>Toss it
          </a>
        </div>` : ''}
        <div class="meta-text mt-1 ms-1">
          ${item.lookup_failed ? '<i class="bi bi-exclamation-triangle text-warning me-1"></i>lookup failed: ' : ''}
          ${item.barcode ? `barcode ${PR.esc(item.barcode)} · ` : ''}
          ${item.brand ? `${PR.esc(item.brand)} · ` : ''}
          scanned ${item.created_at ? item.created_at.replace('T',' ').slice(0,16) : '?'}
          ${item.source ? ` via ${PR.esc(item.source)}` : ''}
        </div>
      </div>
    </div>
  `).join('');
}

async function patchItem(id, field, value, el) {
  try {
    const r = await fetch(`pending/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ [field]: value }),
    });
    const item = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(item.detail || r.statusText);
    if (el) {
      el.classList.add('field-saved');
      setTimeout(() => el.classList.remove('field-saved'), 800);
    }
    // A storage change recomputes the best-by from the new area's shelf-life
    // rule on the server; reflect the returned date so the row shows what
    // will actually commit.
    const dateEl = document.getElementById(`pending-date-${id}`);
    if (field !== 'best_by_date' && dateEl && (item.best_by_date || '') !== dateEl.value) {
      dateEl.value = item.best_by_date || '';
      dateEl.classList.add('field-saved');
      setTimeout(() => dateEl.classList.remove('field-saved'), 800);
    }
  } catch(e) {
    showToast(`Save failed: ${e.message}`, true);
  }
}

// Sniff test on a review row whose product is already in stock and expiring
// (FoodAssistant-8kvp): the same endpoint the Expiring page uses, extending
// every dated stock entry of that product. The pending row itself, the new
// scan waiting to be committed, is deliberately untouched.
async function sniffTest(productId, name, extraDays) {
  try {
    const r = await fetch(`expiring/extend/${productId}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ days: extraDays }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.message || r.statusText);
    showToast(d.new_best_by
      ? `Sniff test passed: ${name} is now best by ${d.new_best_by}.`
      : `Sniff test passed, but ${name} had no best-by date to extend.`);
    await loadPending();
  } catch (e) {
    showToast(`Could not update ${name}: ${e.message}`, true);
  }
}

// Toss on a review row whose product is already in stock and expiring
// (FoodAssistant-64eg): the sniff test's honest opposite. The same endpoint
// the Expiring page uses removes that stock as spoiled; without an amount the
// server tosses what is actually there. The pending row itself, the new scan
// waiting to be committed, is deliberately untouched.
async function tossStock(productId, name) {
  if (!confirm(`Toss the ${name} already in stock? This records it as spoiled and removes it from your stock.`)) return;
  try {
    const r = await fetch(`expiring/toss/${productId}`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.message || r.statusText);
    showToast(`${name} tossed and recorded as spoiled.`);
    await loadPending();
  } catch (e) {
    showToast(`Could not toss ${name}: ${e.message}`, true);
  }
}

async function commitOne(id) {
  await doCommit([id]);
}

async function commitAll() {
  await doCommit(null);
}

async function doCommit(ids) {
  document.getElementById('commit-status').innerHTML =
    '<div class="alert alert-info py-1 px-2 small"><span class="spinner-border spinner-border-sm me-1"></span>Committing…</div>';
  try {
    const r = await fetch('pending/commit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ids }),
    });
    const d = await r.json();
    const errs = (d.results || []).filter(x => x.status === 'error');
    if (errs.length === 0) {
      document.getElementById('commit-status').innerHTML = '';
      showToast(`✓ Added ${d.imported} item(s) to your inventory`);
    } else {
      document.getElementById('commit-status').innerHTML =
        `<div class="alert alert-warning py-1 px-2 small">${d.imported} imported, ${errs.length} failed: ${errs.map(x => PR.esc(x.error)).join('; ')}</div>`;
    }
    await loadPending();
  } catch(e) {
    document.getElementById('commit-status').innerHTML =
      `<div class="alert alert-danger py-1 px-2 small">Commit failed: ${PR.esc(e.message)}</div>`;
  }
}

async function discardItem(id) {
  try {
    await fetch(`pending/${id}`, { method: 'DELETE' });
    document.getElementById(`pending-${id}`)?.remove();
    const cnt = document.getElementById('pending-count');
    cnt.textContent = Math.max(0, parseInt(cnt.textContent) - 1);
    if (cnt.textContent === '0') loadPending();
  } catch(e) {
    showToast(`Delete failed: ${e.message}`, true);
  }
}

function showToast(msg, isError = false) {
  const toast = document.getElementById('pToast');
  toast.className = `toast align-items-center border-0 text-bg-${isError ? 'danger' : 'success'}`;
  document.getElementById('pToastMsg').textContent = msg;
  new bootstrap.Toast(toast, { delay: 3000 }).show();
}

// --- Action items / notifications (FoodAssistant-iut3) -------------------
function actionItemCard(it) {
  const wrap = document.createElement('div');
  const level = ['info', 'success', 'warning', 'error'].includes(it.level) ? it.level : 'info';
  const accent = { info: 'info', success: 'success', warning: 'warning', error: 'danger' }[level];
  wrap.className = 'card border-' + accent;
  const body = document.createElement('div');
  body.className = 'card-body py-2 px-3 d-flex justify-content-between align-items-center flex-wrap gap-2';
  const text = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'fw-semibold';
  title.textContent = it.title || '';
  const sub = document.createElement('div');
  sub.className = 'meta-text';
  sub.textContent = it.body || '';
  text.appendChild(title);
  if (it.body) text.appendChild(sub);
  // A system warning can carry a guide link in its payload (the power and
  // cabling doc for undervoltage); render it as a small trailing link. An
  // app-relative path ("ui/shopping" on a receipt-read item) deep-links to
  // that page in the same tab; anything else in the payload is ignored.
  const url = it.payload && typeof it.payload.url === 'string' ? it.payload.url : '';
  if (url && (url.startsWith('https://') || url.startsWith('http://'))) {
    const a = document.createElement('a');
    a.className = 'meta-text';
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Open the guide';
    text.appendChild(a);
  } else if (url && /^[a-zA-Z0-9][a-zA-Z0-9/_-]*$/.test(url)) {
    const a = document.createElement('a');
    a.className = 'meta-text';
    a.href = url;
    a.textContent = (it.payload && typeof it.payload.url_text === 'string' && it.payload.url_text) || 'Open';
    text.appendChild(a);
  }
  const btns = document.createElement('div');
  btns.className = 'd-flex gap-1';
  // A leftover prompt offers a primary "Save to leftovers"; every item can be
  // snoozed or archived. The save action is wired by the leftovers feature.
  if (it.kind === 'leftover_prompt') {
    btns.appendChild(actionBtn('Save to leftovers', 'success', () => saveLeftover(it)));
  }
  // A paired device that never connected: its key can go from here, for a
  // visitor allowed to manage keys (FoodAssistant-x60df).
  const section = document.getElementById('action-items-section');
  if (it.kind === 'paired_key_unused' && section && section.dataset.managesKeys === '1'
      && it.payload && it.payload.credential_id) {
    btns.appendChild(actionBtn('Remove key', 'outline-danger', () => removeUnusedKey(it)));
  }
  btns.appendChild(actionBtn('Snooze 24h', 'outline-secondary', () => actionItemAct(it.id, 'snooze')));
  btns.appendChild(actionBtn('Archive', 'outline-secondary', () => actionItemAct(it.id, 'archive')));
  body.appendChild(text);
  body.appendChild(btns);
  wrap.appendChild(body);
  return wrap;
}

function actionBtn(label, variant, onclick) {
  const b = document.createElement('button');
  b.className = 'btn btn-sm btn-' + variant;
  b.textContent = label;
  b.onclick = onclick;
  return b;
}

async function loadActionItems() {
  let data = { items: [] };
  try {
    const r = await fetch('action-items', { cache: 'no-store' });
    if (r.ok) data = await r.json();
  } catch (e) { /* offline: leave the section as-is */ return; }
  const section = document.getElementById('action-items-section');
  const list = document.getElementById('action-items-list');
  const count = document.getElementById('action-items-count');
  const items = data.items || [];
  count.textContent = items.length;
  list.innerHTML = '';
  if (!items.length) { section.classList.add('d-none'); return; }
  section.classList.remove('d-none');
  items.forEach(it => list.appendChild(actionItemCard(it)));
}

async function archiveAllActionItems() {
  const n = document.getElementById('action-items-count').textContent;
  if (n !== '0' && !confirm('Archive all ' + n + ' action items?')) return;
  try {
    await fetch('action-items/archive-all', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: '{}' });
  } catch (e) { showToast('Could not archive', true); return; }
  loadActionItems();
}

async function actionItemAct(id, action) {
  try {
    await fetch('action-items/' + id + '/' + action, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: '{}' });
  } catch (e) { showToast('Could not update the item', true); return; }
  loadActionItems();
}

// Remove a paired key nobody has used (FoodAssistant-x60df). The server keeps
// the key when its device has connected since, and closes the item either way.
async function removeUnusedKey(it) {
  const p = it.payload || {};
  const name = typeof p.name === 'string' && p.name ? p.name : 'this device';
  if (!confirm('Remove the key for ' + name + '? The device will need to pair again.')) return;
  let data = {};
  try {
    const r = await fetch('api/credentials/' + encodeURIComponent(p.credential_id) + '/remove-unused', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    try { data = await r.json(); } catch (e) { data = {}; }
    // A key whose device connected since is kept: news, not an error.
    if (r.ok) showToast('Key removed');
    else if (data.used && data.error) showToast(data.error);
    else showToast(data.error || data.detail || 'Could not remove the key', true);
  } catch (e) { showToast('Could not remove the key', true); return; }
  loadActionItems();
}

// Save a cooked meal to Grocy as a short-expiry leftover (FoodAssistant-fu1u),
// then the originating action item resolves itself server-side.
async function saveLeftover(it) {
  const p = it.payload || {};
  try {
    const r = await fetch('current-recipe/leftover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: p.title || '', servings: p.servings || 1,
                             days: p.days || 4, action_item_id: it.id }),
    });
    if (!r.ok) throw new Error('save failed');
    showToast('Saved to leftovers');
  } catch (e) { showToast('Could not save the leftover', true); return; }
  loadActionItems();
}

loadActionItems();
setInterval(() => { if (!document.hidden) loadActionItems(); }, 30000);

loadPending();
// Headless scanners add items while this page is open, so keep it fresh. A
// hidden tab skips the refresh and catches up as soon as it is shown again.
setInterval(() => { if (!document.hidden) loadPending(); }, 15000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { loadPending(); loadActionItems(); }
});
