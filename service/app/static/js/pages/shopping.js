// Shopping list page (templates/shopping.html). Server data arrives in the
// #shopping-data JSON block: the list backend and whether AI is set up.

// One implementation for both list backends: the /mealie/shopping API answers
// the same shapes whether the list lives in Grocy (the default, next to the
// inventory) or in Mealie (installs still running their recipes there).
const SHOPPING_DATA = JSON.parse(document.getElementById('shopping-data').textContent);
const SHOPPING_BACKEND = SHOPPING_DATA.backend;
let currentListId = '';
let itemsById = {};


async function load(listId = '') {
  const el = document.getElementById('items');
  try {
    const r = await fetch('mealie/shopping' + (listId ? '?list_id=' + listId : ''));
    if (!r.ok) throw new Error(await prErrText(r));
    const data = await r.json();
    if (data.error) {
      // Backend down or unconfigured: the API answers with an honest reason
      // instead of an error status, so say that rather than "no lists yet".
      el.innerHTML = `<div class="alert alert-warning"><i class="bi bi-plug me-2"></i>${PR.esc(data.error)}</div>`;
      return;
    }
    if (!data.list) {
      el.innerHTML = '<div class="alert alert-info">No shopping list yet: add an item above to start one.</div>';
      return;
    }
    currentListId = data.list.id;
    itemsById = Object.fromEntries(data.items.map(i => [i.id, i]));

    const sel = document.getElementById('list-select');
    if (data.lists.length > 1) {
      sel.classList.remove('d-none');
      sel.innerHTML = data.lists.map(l =>
        `<option value="${l.id}" ${l.id === currentListId ? 'selected' : ''}>${PR.esc(l.name)}</option>`).join('');
    }

    if (!data.items.length) {
      el.innerHTML = '<div class="text-secondary small p-2">Your shopping list is empty. Type an item above to add it,'
        + ' or pick Shopping list on the Manage page and scan what you are running low on.</div>';
      return;
    }
    el.innerHTML = data.items.map(i => {
      const label = i.display || ((i.quantity && i.quantity !== 1 ? i.quantity + ' \xd7 ' : '') + (i.note || (i.food && i.food.name) || ''));
      return `
      <div class="list-group-item d-flex align-items-center justify-content-between ${i.checked ? 'opacity-50' : ''}">
        <div class="form-check mb-0">
          <input class="form-check-input" type="checkbox" ${i.checked ? 'checked' : ''}
                 data-on-change="toggleItem('${i.id}', this.checked)" id="chk-${i.id}">
          <label class="form-check-label ${i.checked ? 'text-decoration-line-through' : ''}" for="chk-${i.id}">
            ${PR.esc(label)}
          </label>
        </div>
        <button class="btn btn-sm btn-outline-danger btn-icon" data-on-click="removeItem('${i.id}')" title="Remove from the list"
                aria-label="Remove ${PR.esc(label)} from the list">
          <i class="bi bi-x-lg"></i>
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<div class="alert alert-danger">Could not load shopping list: ${PR.esc(e.message)}</div>`;
  }
}

// Every list change used to be sent and forgotten: if it did not work, the
// checkbox sprang back with no explanation and a typed item vanished. Now the
// reason lands in its own slot above the list, and the box keeps what you
// typed so you can just tap add again.
function shoppingMsg(text) {
  const el = document.getElementById('shopping-msg');
  if (!el) return;
  if (!text) { el.classList.add('d-none'); el.textContent = ''; return; }
  el.textContent = text;
  el.classList.remove('d-none');
}

async function toggleItem(id, checked) {
  const item = {...itemsById[id], checked};
  try {
    const r = await fetch('mealie/shopping/items/' + id, {
      method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(item),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    shoppingMsg('');
  } catch (e) {
    shoppingMsg('Could not check that off. ' + prReason(e));
  }
  load(currentListId);
}

async function removeItem(id) {
  try {
    const r = await fetch('mealie/shopping/items/' + id, {method: 'DELETE'});
    if (!r.ok) throw new Error(await prErrText(r));
    shoppingMsg('');
  } catch (e) {
    shoppingMsg('Could not remove that item. ' + prReason(e));
  }
  load(currentListId);
}

async function addItem(ev) {
  ev.preventDefault();
  const input = document.getElementById('new-item');
  const note = input.value.trim();
  if (!note) return;
  try {
    const r = await fetch('mealie/shopping/items', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({list_id: currentListId || '', note}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    input.value = '';
    shoppingMsg('');
  } catch (e) {
    shoppingMsg('Could not add ' + note + '. ' + prReason(e));
  }
  load(currentListId);
}

// Typeahead for the add box: known product/food names from the list's backend.
// Fail-soft: no suggestions just leaves a plain text box.
let suggestTimer = null;
function suggestItems(q) {
  clearTimeout(suggestTimer);
  q = (q || '').trim();
  if (q.length < 2) return;
  suggestTimer = setTimeout(async () => {
    try {
      const r = await fetch('mealie/foods/suggest?q=' + encodeURIComponent(q));
      if (!r.ok) return;
      const d = await r.json();
      document.getElementById('new-item-suggestions').innerHTML =
        (d.suggestions || []).map(n => `<option value="${PR.esc(n)}">`).join('');
    } catch (_) {}
  }, 250);
}

// Grocy list only: clearing checked items and restock suggestions.
async function clearDone() {
  try {
    const r = await fetch('mealie/shopping/clear-done', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({list_id: String(currentListId || '')}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    shoppingMsg('');
  } catch (e) {
    shoppingMsg('Could not clear the checked items. ' + prReason(e));
  }
  load(currentListId);
}

// Restock suggestions: products used up recently that are now out of stock.
async function loadSuggestions() {
  const card = document.getElementById('suggestions-card');
  const el = document.getElementById('suggestions');
  if (!card || !el) return;
  try {
    const r = await fetch('grocy/suggestions');
    if (!r.ok) return;
    const data = await r.json();
    if (!data.items || !data.items.length) return;
    card.classList.remove('d-none');
    el.innerHTML = data.items.map(s => `
      <div class="list-group-item d-flex align-items-center justify-content-between">
        <span class="small">${PR.esc(s.product_name)}
          <span class="text-secondary ms-1">(used ${s.consume_count}x / ${s.days}d)</span>
        </span>
        <button class="btn btn-sm btn-outline-primary btn-icon"
                data-on-click="addSuggestion(${PR.escAttrJs(s.product_name)})"
                title="Add to list">
          <i class="bi bi-plus-lg"></i>
        </button>
      </div>`).join('');
  } catch (_) {}
}

async function addSuggestion(name) {
  try {
    const r = await fetch('mealie/shopping/items', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({list_id: String(currentListId || ''), note: name}),
    });
    if (!r.ok) throw new Error(await prErrText(r));
    shoppingMsg('');
  } catch (e) {
    shoppingMsg('Could not add ' + name + '. ' + prReason(e));
  }
  load(currentListId);
}

if (SHOPPING_BACKEND === 'grocy') loadSuggestions();

// Needs an AI provider.
// Receipt price capture: photo -> /receipt/analyze acks instantly and reads
// the receipt in the background (it takes a minute or two, and the user is
// free to leave the page). The finished result waits server-side, so this
// page renders it on load, polls /receipt/status only while a read is
// running, and /receipt/apply writes each confirmed price onto the matched
// product's newest stock entry.
let receiptItems = [];
let receiptPollTimer = null;

function receiptStatus(html, kind) {
  const el = document.getElementById('receipt-status');
  el.className = 'alert alert-' + (kind || 'info') + ' py-2 mb-2';
  el.innerHTML = html;
}

function showReceiptDismiss(show) {
  document.getElementById('receipt-dismiss').classList.toggle('d-none', !show);
}

// The status poll goes through PR.poller: a slow answer never stacks a second
// request, and the request is dropped when the page is left.
let receiptTick = null;
function startReceiptPolling() {
  if (receiptPollTimer) return;
  if (!receiptTick) receiptTick = PR.poller(checkReceiptStatus);
  receiptPollTimer = setInterval(() => { if (!document.hidden) receiptTick(); }, 5000);
}

function stopReceiptPolling() {
  if (receiptPollTimer) { clearInterval(receiptPollTimer); receiptPollTimer = null; }
}

function showReceiptReading(message) {
  document.getElementById('receipt-card').classList.remove('d-none');
  document.getElementById('receipt-review').classList.add('d-none');
  showReceiptDismiss(false);
  receiptStatus('<i class="bi bi-hourglass-split me-1"></i>' +
    PR.esc(message || 'Reading your receipt. This takes a minute or two. You can leave this page.'));
}

async function receiptSelected(input) {
  const file = input.files[0];
  input.value = '';
  if (!file) return;
  document.getElementById('receipt-card').classList.remove('d-none');
  document.getElementById('receipt-review').classList.add('d-none');
  showReceiptDismiss(false);
  document.getElementById('receipt-btn').disabled = true;
  receiptStatus('Sending the photo…');
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('receipt/analyze', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    showReceiptReading(data.message);
    startReceiptPolling();
  } catch (e) {
    receiptStatus('Could not read the receipt: ' + PR.esc(e.message), 'danger');
    showReceiptDismiss(true);
  } finally {
    document.getElementById('receipt-btn').disabled = false;
  }
}

// On load, and on each poll tick while a read is running: render whatever the
// server holds, so navigating away and back never loses the results.
async function checkReceiptStatus(signal) {
  let data = null;
  try {
    const r = await fetch('receipt/status', { cache: 'no-store', signal });
    if (!r.ok) return;
    data = await r.json();
  } catch (_) { return; }
  if (data.status === 'pending') {
    showReceiptReading(data.message);
    startReceiptPolling();
    return;
  }
  stopReceiptPolling();
  if (data.status === 'ready') {
    document.getElementById('receipt-card').classList.remove('d-none');
    renderReceiptReview(data);
  } else if (data.status === 'failed') {
    document.getElementById('receipt-card').classList.remove('d-none');
    document.getElementById('receipt-review').classList.add('d-none');
    receiptStatus(PR.esc(data.message || 'The receipt could not be read.'), 'warning');
    showReceiptDismiss(true);
  } else {
    // Nothing stored (dismissed or applied elsewhere): put the card away.
    closeReceiptCard();
  }
}

function renderReceiptReview(data) {
  receiptItems = data.items || [];
  showReceiptDismiss(false);
  if (!receiptItems.length) {
    receiptStatus('No priced items could be read from that photo. Try a sharper, straight-on shot.', 'warning');
    showReceiptDismiss(true);
    return;
  }
  document.getElementById('receipt-status').classList.add('d-none');
  document.getElementById('receipt-store').textContent =
    data.store ? 'Receipt from ' + data.store : 'Store name not readable on this receipt.';
  document.getElementById('receipt-rows').innerHTML = receiptItems.map((it, idx) => {
    const usable = it.product_id && it.price;
    const price = it.price ? Number(it.price).toFixed(2) : null;
    const target = it.product_id
      ? '<i class="bi bi-arrow-right mx-1"></i>' + PR.esc(it.product_name)
      : '<span class="text-secondary ms-1">(no match in your pantry)</span>';
    return `
    <label class="list-group-item d-flex align-items-center gap-2 ${usable ? '' : 'opacity-50'}">
      <input class="form-check-input receipt-check" type="checkbox" data-idx="${idx}"
             ${usable ? 'checked' : 'disabled'}>
      <span class="flex-grow-1">${PR.esc(it.name)}${target}</span>
      <span class="${price ? '' : 'text-secondary'}">${price ? price : 'no price'}</span>
    </label>`;
  }).join('');
  document.getElementById('receipt-review').classList.remove('d-none');
}

async function applyReceiptPrices() {
  const pairs = [...document.querySelectorAll('.receipt-check:checked')].map(cb => {
    const it = receiptItems[Number(cb.dataset.idx)];
    return { product_id: it.product_id, price: it.price, name: it.name };
  });
  if (!pairs.length) {
    receiptStatus('Nothing is checked, so there is nothing to apply.', 'warning');
    return;
  }
  const btn = document.getElementById('receipt-apply-btn');
  btn.disabled = true;
  try {
    const r = await fetch('receipt/apply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ pairs }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    document.getElementById('receipt-review').classList.add('d-none');
    receiptItems = [];
    let html = `Recorded <strong>${data.applied}</strong> price(s).`;
    if (data.failed && data.failed.length) {
      html += '<ul class="mb-0 mt-1">' + data.failed.map(f =>
        `<li>${PR.esc(f.name)}: ${PR.esc(f.reason)}</li>`).join('') + '</ul>';
    }
    receiptStatus(html, data.failed && data.failed.length ? 'warning' : 'success');
    showReceiptDismiss(true);
  } catch (e) {
    receiptStatus('Could not apply the prices: ' + PR.esc(e.message), 'danger');
  } finally {
    btn.disabled = false;
  }
}

// Clears the stored result server-side too (and archives its Review inbox
// note), so the card does not come back on the next visit.
async function dismissReceipt() {
  stopReceiptPolling();
  try { await fetch('receipt/dismiss', { method: 'POST' }); } catch (_) {}
  closeReceiptCard();
}

function closeReceiptCard() {
  stopReceiptPolling();
  document.getElementById('receipt-card').classList.add('d-none');
  document.getElementById('receipt-review').classList.add('d-none');
  showReceiptDismiss(false);
  receiptItems = [];
}

if (SHOPPING_DATA.ai) checkReceiptStatus();

load();
