// Settings > Bandit Remotes > Screens: the tablets and wall panels that signed
// up with a screen key (GET setup/screens). Each row renames the screen, turns
// its Wall tablet layout on or off, or removes it. Loaded when the pane opens
// and on Refresh; nothing here runs on a timer.

function _screenSeen(s) {
  if (s.online) return '<span class="badge bg-success">Online</span>';
  const when = s.last_seen ? fmtSyncTime(s.last_seen) : '';
  return '<span class="badge bg-secondary">Offline</span>'
    + (when ? ` <span class="small text-secondary">Last seen ${PR.esc(when)}</span>` : '');
}

function screenRowHtml(s) {
  const id = PR.esc(s.id);
  const idArg = PR.escAttrJsStr(s.id);
  const size = s.width && s.height
    ? ` <span class="small text-secondary">${PR.esc(String(s.width))} by ${PR.esc(String(s.height))}</span>` : '';
  return `<div class="border rounded p-2 mb-2" data-screen-row="${id}">
    <div class="d-flex flex-wrap align-items-center gap-2">
      <input type="text" class="form-control form-control-sm" style="max-width:220px" maxlength="60"
        id="screen-name-${id}" value="${PR.esc(s.label)}" aria-label="Name of this screen"
        data-on-change="renameScreen(${idArg})">
      <span class="small">${PR.esc(s.kind_label || s.kind)}</span>${size}
      <span class="ms-auto">${_screenSeen(s)}</span>
      <button class="btn btn-outline-danger btn-sm" data-on-click="removeScreen(${idArg})"
        title="Remove this screen" aria-label="Remove ${PR.esc(s.label)}"><i class="bi bi-trash"></i></button>
    </div>
    <div class="d-flex flex-wrap align-items-center gap-2 mt-2">
      <label class="set-toggle">
        <input type="checkbox" role="switch" id="screen-wall-${id}"${s.wall ? ' checked' : ''}
          data-on-change="setScreenWall(${idArg}, this)">
        <span class="set-toggle-track"><span class="set-toggle-thumb"></span></span>
      </label>
      <label class="small mb-0" for="screen-wall-${id}">Wall tablet: bigger text, a short menu and fewer buttons</label>
      <span class="test-result small" id="screen-result-${id}" role="status"></span>
    </div>
  </div>`;
}

async function loadScreens() {
  const out = document.getElementById('screens-list');
  if (!out) return;
  try {
    const r = await fetch('setup/screens', { cache: 'no-store' });
    const d = await readJsonResponse(r);
    const list = d.screens || [];
    if (!list.length) {
      out.innerHTML = '<span class="text-secondary small">No tablets or wall panels yet. One shows up here once its app connects with a screen key.</span>';
      return;
    }
    out.innerHTML = list.map(screenRowHtml).join('');
  } catch (e) {
    out.innerHTML = `<span class="text-danger small">${PR.esc(e.message || String(e))}</span>`;
  }
}

async function _editScreen(id, body) {
  const el = document.getElementById(`screen-result-${id}`);
  if (el) { el.className = 'test-result small text-secondary'; el.textContent = 'Saving...'; }
  try {
    const r = await fetch(`setup/screens/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await readJsonResponse(r);
    if (!d.ok) throw new Error(d.error || 'Could not save.');
    if (el) {
      el.className = 'test-result small text-success';
      el.textContent = 'wall' in body ? 'Saved. The screen changes on its next page.' : 'Saved.';
    }
    return d.screen || null;
  } catch (e) {
    if (el) { el.className = 'test-result small text-danger'; el.textContent = e.message || 'Could not save.'; }
    return null;
  }
}

async function renameScreen(id) {
  const inp = document.getElementById(`screen-name-${id}`);
  if (!inp) return;
  const saved = await _editScreen(id, { label: inp.value.trim() });
  if (saved) inp.value = saved.label;
}

async function setScreenWall(id, box) {
  box.disabled = true;
  const saved = await _editScreen(id, { wall: box.checked });
  if (saved) box.checked = !!saved.wall;
  else box.checked = !box.checked;   // the save failed; show what is stored
  box.disabled = false;
}

async function removeScreen(id) {
  if (!(await PR.confirmBox(
      'It comes back the next time its app starts. To keep it off, turn its key off in Settings, Security.',
      { title: 'Remove this screen?', ok: 'Remove', danger: true }))) return;
  try {
    await fetch(`setup/screens/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch (e) { /* the list below shows what is there */ }
  loadScreens();
}

document.addEventListener('DOMContentLoaded', function () {
  if (!document.getElementById('screens-list')) return;
  const pill = document.querySelector('.side-menu [data-bs-target="#pane-devices"]');
  if (pill) pill.addEventListener('shown.bs.tab', loadScreens);
  const pane = document.getElementById('pane-devices');
  if (pane && pane.classList.contains('active')) loadScreens();
});
