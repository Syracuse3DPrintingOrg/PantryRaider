// Expiry defaults page (templates/defaults.html).

function filterTable() {
  const cat = document.getElementById('filterCategory').value.toLowerCase();
  const stor = document.getElementById('filterStorage').value.toLowerCase();
  const search = document.getElementById('filterSearch').value.toLowerCase();
  document.querySelectorAll('#defaultsTable tbody tr').forEach(row => {
    const matchCat  = !cat  || row.dataset.category.toLowerCase() === cat;
    const matchStor = !stor || row.dataset.storage.toLowerCase() === stor;
    const matchName = !search || row.dataset.name.toLowerCase().includes(search);
    row.style.display = (matchCat && matchStor && matchName) ? '' : 'none';
  });
}

function openEdit(id, category, name, storage, days, notes) {
  const form = document.getElementById('editForm');
  form.action = `ui/defaults/${id}/update`;
  form.querySelector('[name=category]').value = category;
  form.querySelector('[name=name_pattern]').value = name;
  form.querySelector('[name=storage_type]').value = storage;
  form.querySelector('[name=default_days]').value = days;
  form.querySelector('[name=notes]').value = notes;
  new bootstrap.Modal(document.getElementById('editModal')).show();
}

// Delegated so the edit buttons carry no inline handler: rule text stays data,
// never code. Delegation also survives the rows being re-rendered.
document.addEventListener('click', function (ev) {
  const btn = ev.target.closest('[data-edit-rule]');
  if (!btn) return;
  const d = btn.dataset;
  openEdit(d.id, d.category || '', d.name || '', d.storage || '', d.days || '', d.notes || '');
});
