// Cooking journal page (templates/journal.html).
const TYPE_LABELS = {
  'purchase': {label: 'Added', cls: 'success'},
  'consume': {label: 'Consumed', cls: 'warning'},
  'product-opened': {label: 'Opened', cls: 'info'},
  'inventory-correction': {label: 'Corrected', cls: 'secondary'},
  'self-production': {label: 'Produced', cls: 'primary'},
  'transfer': {label: 'Moved', cls: 'info'},
  'waste': {label: 'Discarded', cls: 'danger'},
};

let allEntries = [];

function applyFilter() {
  const q = document.getElementById('filter-input').value.toLowerCase();
  const rows = allEntries.filter(e => !q || e.product_name.toLowerCase().includes(q));
  renderRows(rows);
}

function renderRows(entries) {
  const wrap = document.getElementById('journal-table-wrap');
  if (!entries.length) {
    wrap.innerHTML = '<div class="text-secondary small p-2">No entries found.</div>';
    return;
  }
  const rows = entries.map(e => {
    const t = TYPE_LABELS[e.transaction_type] || {label: e.transaction_type || 'Unknown', cls: 'secondary'};
    const amtClass = e.amount >= 0 ? 'text-success' : 'text-warning';
    const amtStr = (e.amount >= 0 ? '+' : '') + e.amount.toFixed(1).replace(/\.0$/, '');
    const note = e.note ? `<span class="text-secondary small ms-2">${PR.esc(e.note)}</span>` : '';
    return `<tr>
      <td class="text-secondary small text-nowrap">${PR.esc(e.timestamp.replace('T', ' '))}</td>
      <td>${PR.esc(e.product_name)}</td>
      <td><span class="badge text-bg-${t.cls}">${t.label}</span></td>
      <td class="${amtClass} text-end">${amtStr}</td>
      <td>${note}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm table-hover align-middle">
        <thead class="table-dark sticky-top">
          <tr>
            <th>Date/Time</th>
            <th>Product</th>
            <th>Type</th>
            <th class="text-end">Qty</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}


async function load() {
  const limit = document.getElementById('limit-select').value;
  const wrap = document.getElementById('journal-table-wrap');
  wrap.innerHTML = '<div class="text-secondary"><span class="spinner-border spinner-border-sm me-2"></span>Loading…</div>';
  try {
    const r = await fetch(`grocy/stock-log?limit=${limit}`);
    if (!r.ok) throw new Error(await prErrText(r));
    const data = await r.json();
    allEntries = data.entries || [];
    applyFilter();
  } catch(e) {
    wrap.innerHTML = `<div class="alert alert-danger">Could not load stock log: ${e.message}</div>`;
  }
}

load();
