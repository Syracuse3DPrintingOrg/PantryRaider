// Inventory page (templates/inventory.html).
// Whether label printing is turned on (FoodAssistant-fb8x). When off, no print
// affordance renders at all, so the feature is invisible until enabled.
const INVENTORY_DATA = JSON.parse(document.getElementById('inventory-data').textContent);
const PRINTING_ENABLED = INVENTORY_DATA.printing_enabled;

// Panels (built-in + custom + "other"), injected from the server-side registry.
const PANELS = INVENTORY_DATA.panels;
const PANEL_KEYS = PANELS.map(c => c.key);
const BUCKET_LABELS = Object.fromEntries(PANELS.map(c => [c.key, c.label]));

const URGENCY_BADGE = {
  expired: ['badge-expired', '⚠ Expired'],
  today:   ['badge-today',   'Expires today'],
  '3d':    ['badge-3d',      null],  // filled dynamically
  '7d':    ['badge-7d',      null],
  ok:      ['badge-ok',      null],
  unknown: ['badge-unknown', 'No date'],
};

function expiryBadge(item) {
  const u = item.urgency;
  const cls = (URGENCY_BADGE[u] || URGENCY_BADGE.unknown)[0];
  let label;
  if (u === 'expired') {
    label = `⚠ ${Math.abs(item.days_remaining)}d ago`;
  } else if (u === 'today') {
    label = 'Today';
  } else if (item.days_remaining != null) {
    label = `${item.days_remaining}d`;
  } else {
    label = 'No date';
  }
  return `<span class="expiry-badge ${cls}">${label}</span>`;
}


function renderPanel(panelId, countId, items, bucket) {
  const el = document.getElementById(panelId);
  const cnt = document.getElementById(countId);
  const showAdded = document.getElementById('sortSelect').value.startsWith('added');
  cnt.textContent = items.length;
  if (items.length === 0) {
    el.innerHTML = '<div class="panel-empty"><i class="bi bi-check-circle text-success me-1"></i>Empty</div>';
    return;
  }
  el.innerHTML = items.map(item => `
    <div class="item-row" id="row-${item.product_id}" draggable="true"
         data-product-id="${item.product_id}" data-bucket="${bucket}" data-name="${PR.esc(item.name)}"
         data-on-dragstart="dragStart(event)" data-on-dragend="dragEnd(event)">
      <div class="item-name">
        <div class="item-name-text" title="${PR.esc(item.name)}${item.added_date ? ', added ' + item.added_date.slice(0,10) : ''}">${PR.esc(item.name)}</div>
      </div>
      ${showAdded && item.added_date ? `<span class="item-qty">${item.added_date.slice(0,10)}</span>` : ''}
      <span class="item-qty">${item.amount % 1 === 0 ? item.amount : item.amount.toFixed(1)}${item.unit ? ' ' + PR.esc(item.unit) : ''}</span>
      ${item.amount_opened > 0 ? `<span class="expiry-badge badge-opened" title="${item.amount_opened % 1 === 0 ? item.amount_opened : item.amount_opened.toFixed(1)} of these ${PR.esc(item.name)} already open">Opened</span>` : ''}
      ${expiryBadge(item)}
      <div class="item-actions">
      <button class="btn btn-outline-secondary btn-icon" title="Edit category / expiry"
              aria-label="Edit ${PR.esc(item.name)}"
              data-on-click="openEditModal(${item.product_id})">
        <i class="bi bi-pencil"></i>
      </button>
      <button class="btn btn-outline-secondary btn-icon" title="Move to another location"
              aria-label="Move ${PR.esc(item.name)} to another location"
              data-on-click="openMoveModal(${item.product_id}, ${PR.escAttrJs(item.name)})">
        <i class="bi bi-arrow-left-right"></i>
      </button>
      ${item.amount > 0 ? `
      <button class="btn btn-outline-secondary btn-icon" title="Mark one ${PR.esc(item.name)} opened"
              data-on-click="markOpened(${item.product_id}, ${PR.escAttrJs(item.name)})">
        <i class="bi bi-box2"></i>
      </button>` : ''}
      <button class="btn btn-outline-secondary btn-icon" title="Mark consumed"
              aria-label="Mark ${PR.esc(item.name)} consumed"
              data-on-click="consume(${item.product_id}, ${item.amount}, ${PR.escAttrJs(item.name)})">
        <i class="bi bi-check2"></i>
      </button>
      ${PRINTING_ENABLED ? `
      <button class="btn btn-outline-secondary btn-icon" title="Print label"
              aria-label="Print a label for ${PR.esc(item.name)}"
              data-on-click="printLabel(${item.product_id}, ${PR.escAttrJs(item.name)})">
        <i class="bi bi-printer"></i>
      </button>` : ''}
      </div>
    </div>
  `).join('');
}

// Item lookup map (keyed by product_id) for pre-filling modals
const itemsById = {};

function storeItems(items) {
  items.forEach(i => { itemsById[i.product_id] = i; });
}

// Edit modal
let editTarget = null;  // product_id

function openEditModal(productId) {
  const item = itemsById[productId];
  if (!item) return;
  editTarget = productId;
  document.getElementById('editModalName').textContent = item.name;

  const catSel = document.getElementById('editCategory');
  catSel.value = item.category || '';
  if (!catSel.value && item.category) {
    // category exists but wasn't in the list, so add a temporary option
    const opt = new Option(item.category, item.category, true, true);
    catSel.add(opt);
  }

  document.getElementById('editBestBy').value = item.best_before_date || '';
  new bootstrap.Modal(document.getElementById('editModal')).show();
}

async function saveEdit() {
  if (editTarget == null) return;
  const btn = document.getElementById('editSaveBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';

  const category = document.getElementById('editCategory').value || null;
  const bestBy   = document.getElementById('editBestBy').value || null;

  try {
    const r = await fetch(`inventory/edit/${editTarget}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, best_before_date: bestBy }),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    bootstrap.Modal.getInstance(document.getElementById('editModal')).hide();
    showToast('✓ Item updated');
    await loadDashboard();
  } catch(e) {
    showToast(`Save failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-check2 me-1"></i>Save';
    editTarget = null;
  }
}

// Drag & Drop
let dragged = null;  // { productId, fromBucket, name }

function dragStart(e) {
  const row = e.currentTarget;
  dragged = {
    productId: parseInt(row.dataset.productId),
    fromBucket: row.dataset.bucket,
    name: row.dataset.name,
  };
  row.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', row.dataset.productId);
}

function dragEnd(e) {
  e.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.drop-target').forEach(p => p.classList.remove('drop-target'));
}

function dragOver(e) {
  if (!dragged) return;
  const panel = e.currentTarget;
  if (panel.dataset.bucket !== dragged.fromBucket) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    panel.classList.add('drop-target');
  }
}

function dragLeave(e) {
  e.currentTarget.classList.remove('drop-target');
}

async function dropItem(e) {
  e.preventDefault();
  const panel = e.currentTarget;
  panel.classList.remove('drop-target');
  if (!dragged || panel.dataset.bucket === dragged.fromBucket) return;
  const { productId, name } = dragged;
  dragged = null;
  await moveItem(productId, panel.dataset.bucket, name);
}

// Move via modal (touch devices)
let moveTarget = null;  // { productId, name }

function openMoveModal(productId, name) {
  moveTarget = { productId, name };
  document.getElementById('moveModalName').textContent = name;
  new bootstrap.Modal(document.getElementById('moveModal')).show();
}

async function moveFromModal(bucket) {
  bootstrap.Modal.getInstance(document.getElementById('moveModal')).hide();
  if (!moveTarget) return;
  const { productId, name } = moveTarget;
  moveTarget = null;
  await moveItem(productId, bucket, name);
}

async function moveItem(productId, bucket, name) {
  try {
    const r = await fetch(`inventory/move/${productId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bucket }),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    // Crossing temperature natures adjusts the best-by (freezing extends,
    // thawing shortens, FoodAssistant-jty6); say so in the confirmation.
    const d = await r.json().catch(() => ({}));
    let msg = `✓ Moved ${name} to ${BUCKET_LABELS[bucket]}`;
    if (d.new_best_by && d.best_by_change) {
      const nice = new Date(`${d.new_best_by}T00:00:00`)
        .toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      msg += `, best-by ${d.best_by_change} to ${nice}`;
    }
    showToast(msg);
    await loadDashboard();
  } catch(e) {
    showToast(`Move failed: ${e.message}`, true);
  }
}

async function loadDashboard() {
  const sort = document.getElementById('sortSelect').value;
  document.getElementById('loading-spinner').style.display = '';
  const status = document.getElementById('inventory-status');
  try {
    const r = await fetch(`inventory/dashboard?sort=${sort}`);
    if (!r.ok) {
      // Outages answer with a JSON detail written for people ("Your inventory
      // is not reachable right now..."), so surface that, never a raw error
      // page. A Grocy setup problem (a config.php that needs an edit) brings
      // its fix hint and, on a Pi appliance, the one-click repair; the "comes
      // back on its own" line is only for a plain connection failure.
      const body = await r.json().catch(() => ({}));
      const err = new Error(body.detail || 'The inventory list did not load. It usually comes back on its own; Refresh retries now.');
      err.hint = body.hint || '';
      err.repairable = !!body.repairable;
      throw err;
    }
    const data = await r.json();
    status.classList.add('d-none');
    status.classList.remove('d-flex');
    const allItems = PANEL_KEYS.flatMap(k => data[k] || []);
    storeItems(allItems);
    PANEL_KEYS.forEach(k => renderPanel(`panel-${k}`, `count-${k}`, data[k] || [], k));
  } catch(e) {
    document.getElementById('inventory-status-text').textContent = e.message;
    const hint = document.getElementById('inventory-status-hint');
    hint.textContent = e.hint || '';
    hint.classList.toggle('d-none', !e.hint);
    document.getElementById('inventory-repair').classList.toggle('d-none', !e.repairable);
    status.classList.remove('d-none');
    status.classList.add('d-flex');
    PANEL_KEYS.forEach(b => {
      document.getElementById(`panel-${b}`).innerHTML =
        '<div class="panel-empty">Unavailable right now</div>';
      document.getElementById(`count-${b}`).textContent = '–';
    });
  } finally {
    document.getElementById('loading-spinner').style.display = 'none';
  }
}

// Mark one unit opened (FoodAssistant-oyef). Grocy applies the product's
// after-opening shelf life, so the best-by date can move; reloading the
// dashboard shows the new date and the Opened badge together.
async function markOpened(productId, name) {
  try {
    const r = await fetch(`inventory/open/${productId}`, { method: 'POST' });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      throw new Error(detail || r.statusText);
    }
    showToast(`✓ Marked one ${name} opened`);
    await loadDashboard();
  } catch(e) {
    showToast(`Could not mark ${name} opened: ${e.message}`, true);
  }
}

async function consume(productId, amount, name) {
  // One tap takes the whole amount out of stock and there is no undo, so ask
  // first (the Expiring page asks the same question).
  const ok = await PR.confirmBox(`Mark all ${name} as consumed? This removes it from your stock.`,
                                 { ok: 'Mark consumed' });
  if (!ok) return;
  try {
    // The JSON endpoint, so a refusal from Grocy reaches the toast instead of
    // hiding behind the page redirect the Expiring form uses.
    const r = await fetch(
      `inventory/consume/${productId}?amount=${encodeURIComponent(amount)}`,
      { method: 'POST' });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail;
      throw new Error(detail || r.statusText);
    }
    const row = document.getElementById(`row-${productId}`);
    if (row) row.remove();
    showToast(`✓ Consumed: ${name}`);
    // Update panel count
    PANEL_KEYS.forEach(b => {
      const panel = document.getElementById(`panel-${b}`);
      const rows = panel.querySelectorAll('.item-row').length;
      if (rows === 0) renderPanel(`panel-${b}`, `count-${b}`, []);
      else document.getElementById(`count-${b}`).textContent = rows;
    });
  } catch(e) {
    showToast(`Could not mark ${name} consumed: ${e.message}`, true);
  }
}

// Print a food label for one item (FoodAssistant-fb8x). The server pulls the
// item's name, added date, and best-by date from Grocy, so the button only
// needs the product id.
async function printLabel(productId, name) {
  try {
    const r = await fetch('printing/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id: productId }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.error || r.statusText);
    showToast(`✓ Label sent for ${name}`);
  } catch(e) {
    showToast(`Print failed: ${e.message}`, true);
  }
}

function showToast(msg, isError = false) {
  const toast = document.getElementById('consumeToast');
  toast.className = `toast align-items-center border-0 text-bg-${isError ? 'danger' : 'success'}`;
  document.getElementById('toastMsg').textContent = msg;
  new bootstrap.Toast(toast, { delay: 3000 }).show();
}

// Collapsible panels
function togglePanel(bucket) {
  const panel = document.querySelector(`[data-bucket="${bucket}"]`);
  const isCollapsed = panel.classList.toggle('panel-collapsed');
  syncPanelExpanded(panel);
  const collapsed = JSON.parse(localStorage.getItem('inv_collapsed') || '[]');
  const updated = isCollapsed
    ? [...new Set([...collapsed, bucket])]
    : collapsed.filter(b => b !== bucket);
  localStorage.setItem('inv_collapsed', JSON.stringify(updated));
}

function syncPanelExpanded(panel) {
  const header = panel.querySelector('[data-toggle-panel]');
  if (header) header.setAttribute('aria-expanded', panel.classList.contains('panel-collapsed') ? 'false' : 'true');
}

function restoreCollapsed() {
  const collapsed = JSON.parse(localStorage.getItem('inv_collapsed') || '[]');
  collapsed.forEach(bucket => {
    const panel = document.querySelector(`[data-bucket="${bucket}"]`);
    if (panel) panel.classList.add('panel-collapsed');
  });
  document.querySelectorAll('#panels [data-bucket]').forEach(syncPanelExpanded);
}

// Panel headers and move buttons carry their bucket id as data, never as code
// spliced into an inline handler.
// The headers are buttons for the keyboard too: Enter or Space toggles one.
document.addEventListener('keydown', function (ev) {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const header = ev.target.closest('[data-toggle-panel]');
  if (!header || ev.target !== header) return;
  ev.preventDefault();
  togglePanel(header.dataset.togglePanel);
});
document.addEventListener('click', function (ev) {
  const header = ev.target.closest('[data-toggle-panel]');
  if (header) { togglePanel(header.dataset.togglePanel); return; }
  const moveBtn = ev.target.closest('[data-move-to]');
  if (moveBtn) moveFromModal(moveBtn.dataset.moveTo);
});

// Initial load
restoreCollapsed();
loadDashboard();
