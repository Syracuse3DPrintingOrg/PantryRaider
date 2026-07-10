// Navigation tab editor (FoodAssistant-81yi). navState carries every tab
// (built-in, custom link, and custom heading/folder) with its hidden flag and
// parent assignment, plus, for custom tabs, the editable label/icon/url. A
// heading has heading=true and no url; it exists only to group children, and it
// renders as a label-only dropdown in the navbar. Nesting is one level deep:
// only a top-level entry can be a parent.
let navState = TABS.map(t => ({key: t.key, label: t.label, icon: t.icon,
                               hidden: t.hidden, available: t.available,
                               custom: !!t.custom, heading: !!t.heading,
                               parent: t.parent || '',
                               url: t.href || ''}));

const tabByKey = (k) => navState.find(t => t.key === k);
// A heading can never be nested (it must stay top-level to hold children), so
// only non-heading entries are allowed to carry a parent.
const canHaveParent = (t) => !t.heading;
// Anything top-level that can hold children is a valid parent target.
const isParentable = (t) => !t.parent;

// Re-order navState so every child sits directly after its parent. The editor
// stores a flat list (parents nest by the parent field, matching the server),
// so a tidy visual tree means grouping children under their parent in order.
function groupedOrder() {
  const out = [];
  navState.forEach(t => {
    if (t.parent) return; // children are emitted under their parent below
    out.push(t);
    navState.forEach(c => { if (c.parent === t.key) out.push(c); });
  });
  // Any child whose parent vanished (defensive) falls back to the end.
  navState.forEach(t => { if (!out.includes(t)) out.push(t); });
  return out;
}

function renderNavEditor() {
  const el = document.getElementById('nav-editor');
  if (!el) return;
  navState = groupedOrder();
  el.innerHTML = navState.map((t, i) => {
    const child = !!t.parent;
    const parent = tabByKey(t.parent);
    // Indent is allowed only when there is a top-level sibling directly above to
    // nest under. Outdent is allowed only for a current child.
    const prev = navState[i - 1];
    const canIndent = canHaveParent(t) && !child && prev &&
                      isParentable(prev) && prev.key !== t.key;
    const canOutdent = child;
    const kindBadge = t.heading
      ? '<span class="badge bg-warning text-dark ms-1" title="A heading (folder); groups other tabs and has no page of its own">folder</span>'
      : (t.custom ? '<span class="badge bg-info ms-1" title="A custom tab you added">custom</span>' : '');
    return `
    <div class="nav-tab-row d-flex align-items-center gap-2 px-2 py-1 ${t.hidden ? 'opacity-50' : ''} ${child ? 'nav-tab-child' : ''}"
         draggable="true" data-key="${t.key}" data-idx="${i}"
         ondragstart="navDragStart(event, ${i})" ondragover="navDragOver(event, ${i})"
         ondragleave="navDragLeave(event)" ondrop="navDrop(event, ${i})" ondragend="navDragEnd(event)">
      <i class="bi bi-grip-vertical text-secondary" style="cursor:grab" title="Drag to reorder"></i>
      <div class="btn-group btn-group-sm" role="group" aria-label="Move tab">
        <button class="btn btn-outline-secondary py-0 px-1" onclick="moveTab(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Move up"
                aria-label="Move up"><i class="bi bi-chevron-up"></i></button>
        <button class="btn btn-outline-secondary py-0 px-1" onclick="moveTab(${i}, 1)" ${i === navState.length - 1 ? 'disabled' : ''} title="Move down"
                aria-label="Move down"><i class="bi bi-chevron-down"></i></button>
        <button class="btn btn-outline-secondary py-0 px-1" onclick="indentTab(${i})" ${canIndent ? '' : 'disabled'} title="Indent (nest under the tab above)"
                aria-label="Indent"><i class="bi bi-arrow-bar-right"></i></button>
        <button class="btn btn-outline-secondary py-0 px-1" onclick="outdentTab(${i})" ${canOutdent ? '' : 'disabled'} title="Outdent (move back to top level)"
                aria-label="Outdent"><i class="bi bi-arrow-bar-left"></i></button>
      </div>
      <i class="bi ${t.icon}"></i>
      <span class="flex-grow-1">${t.label}
        ${kindBadge}
        ${child && parent ? `<span class="text-secondary small ms-1" title="Nested under ${parent.label}"><i class="bi bi-arrow-return-right"></i> ${parent.label}</span>` : ''}
        ${!t.available ? '<span class="badge bg-secondary ms-1" title="Hidden automatically until its service is configured">auto-hidden</span>' : ''}
      </span>
      <div class="form-check form-switch mb-0" title="Show this tab">
        <input class="form-check-input" type="checkbox" ${t.hidden ? '' : 'checked'}
               onchange="navState[${i}].hidden = !this.checked; renderNavEditor()">
      </div>
      ${t.custom ? `<button class="btn btn-outline-danger btn-sm py-0 px-1" title="Remove this ${t.heading ? 'heading' : 'custom tab'}"
               onclick="removeCustomTab('${t.key}')"><i class="bi bi-trash"></i></button>` : ''}
    </div>`;
  }).join('');
}

function moveTab(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= navState.length) return;
  [navState[i], navState[j]] = [navState[j], navState[i]];
  renderNavEditor();
}

// Indent row i: nest it under the nearest top-level entry above it.
function indentTab(i) {
  const t = navState[i];
  if (!t || t.parent || t.heading) return;
  let j = i - 1;
  while (j >= 0 && navState[j].parent) j--; // skip over other children
  const target = navState[j];
  if (!target || !isParentable(target)) return;
  t.parent = target.key;
  renderNavEditor();
}

// Outdent row i back to top level.
function outdentTab(i) {
  const t = navState[i];
  if (!t || !t.parent) return;
  t.parent = '';
  renderNavEditor();
}

// Native HTML5 drag-and-drop. Dragging reorders the flat list; dropping a row
// ONTO a top-level parentable row nests it, dropping elsewhere reorders.
let navDragIdx = null;
function navDragStart(ev, i) {
  navDragIdx = i;
  ev.dataTransfer.effectAllowed = 'move';
  try { ev.dataTransfer.setData('text/plain', String(i)); } catch (e) {}
}
// Which folder key (if any) dropping the dragged row onto this target should
// nest it under. Returns '' when the drop is a plain reorder. Dropping onto a
// top-level parent nests under it; dropping onto a row that is ALREADY inside a
// folder nests under that same folder, so the whole folder region (parent row
// and its children) is a valid "drop in here" target. Headings never nest.
function nestTargetKey(target, dragged) {
  if (!target || !dragged || dragged.heading) return '';
  if (target.key === dragged.key) return '';
  // Nesting is one level deep: a row that already has children cannot also
  // become someone's child.
  if (navState.some(t => t.parent === dragged.key)) return '';
  if (isParentable(target)) return target.key;          // dropped on the folder row itself
  if (target.parent && target.parent !== dragged.key) return target.parent; // dropped on a sibling inside a folder
  return '';
}
function navDragOver(ev, i) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
  const row = ev.currentTarget;
  document.querySelectorAll('.nav-tab-row.nav-drop-into, .nav-tab-row.nav-drop-before')
    .forEach(r => r.classList.remove('nav-drop-into', 'nav-drop-before'));
  const target = navState[i];
  const dragged = navState[navDragIdx];
  // Conventional tree affordance: the middle band of a row nests, the top and
  // bottom edges reorder. Much easier to hit than the old right-edge sliver.
  const r = row.getBoundingClientRect();
  const y = (ev.clientY - r.top) / r.height;
  const edge = y < 0.28 || y > 0.72;            // near an edge -> reorder
  if (!edge && nestTargetKey(target, dragged)) {
    row.classList.add('nav-drop-into');
  } else {
    row.classList.add('nav-drop-before');
  }
}
function navDragLeave(ev) {
  ev.currentTarget.classList.remove('nav-drop-into', 'nav-drop-before');
}
function navDrop(ev, i) {
  ev.preventDefault();
  const into = ev.currentTarget.classList.contains('nav-drop-into');
  ev.currentTarget.classList.remove('nav-drop-into', 'nav-drop-before');
  if (navDragIdx === null || navDragIdx === i) return;
  const dragged = navState[navDragIdx];
  const target = navState[i];
  const nestKey = into ? nestTargetKey(target, dragged) : '';
  if (nestKey) {
    // Nest under the resolved folder and place directly after that folder's
    // last current child so it lands inside the group, not above it.
    dragged.parent = nestKey;
    navState.splice(navDragIdx, 1);
    const parent = tabByKey(nestKey);
    let insertAt = navState.indexOf(parent) + 1;
    while (insertAt < navState.length && navState[insertAt].parent === nestKey) insertAt++;
    navState.splice(insertAt, 0, dragged);
  } else {
    // Plain reorder to the target slot, back at top level.
    dragged.parent = '';
    const item = navState.splice(navDragIdx, 1)[0];
    const ti = navState.indexOf(target);
    navState.splice(ti, 0, item);
  }
  navDragIdx = null;
  renderNavEditor();
}
function navDragEnd(ev) {
  navDragIdx = null;
  document.querySelectorAll('.nav-tab-row.nav-drop-into, .nav-tab-row.nav-drop-before')
    .forEach(r => r.classList.remove('nav-drop-into', 'nav-drop-before'));
}

function addCustomTab() {
  const label = (document.getElementById('custom-tab-label')?.value || '').trim();
  const url = (document.getElementById('custom-tab-url')?.value || '').trim();
  const icon = (document.getElementById('custom-tab-icon')?.value || '').trim() || 'bi-link-45deg';
  const hint = document.getElementById('custom-tab-hint');
  if (!label || !url) {
    if (hint) hint.textContent = 'Enter a label and a URL or route.';
    return;
  }
  pushCustom({label, icon, url, heading: false});
  ['custom-tab-label', 'custom-tab-url', 'custom-tab-icon'].forEach(id => {
    const f = document.getElementById(id); if (f) f.value = '';
  });
  if (hint) hint.textContent = 'Added. Save Theme & Navigation to keep it.';
  renderNavEditor();
}

// Add a heading/folder: a label-only grouping entry with no page. Drag tabs onto
// it (or indent them under it) to fill it. An empty heading is hidden until it
// has children, so it never shows as a dead dropdown.
function addHeading() {
  const label = (document.getElementById('custom-heading-label')?.value || '').trim();
  const icon = (document.getElementById('custom-heading-icon')?.value || '').trim() || 'bi-folder';
  const hint = document.getElementById('custom-heading-hint');
  if (!label) {
    if (hint) hint.textContent = 'Enter a name for the heading.';
    return;
  }
  pushCustom({label, icon, url: '', heading: true});
  const f = document.getElementById('custom-heading-label'); if (f) f.value = '';
  const fi = document.getElementById('custom-heading-icon'); if (fi) fi.value = '';
  if (hint) hint.textContent = 'Heading added. Indent tabs or drag them onto it, then Save.';
  renderNavEditor();
}

// Push a new custom entry with a session-unique key; the server re-derives a
// stable, prefixed id on save, so this only needs to be unique in the editor.
function pushCustom({label, icon, url, heading}) {
  let base = 'custom_' + label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  let key = base, n = 2;
  while (navState.some(t => t.key === key)) { key = base + '_' + (n++); }
  navState.push({key, label, icon, hidden: false, available: true,
                 custom: true, heading: !!heading, parent: '', url});
}

function removeCustomTab(key) {
  navState = navState.filter(t => t.key !== key);
  // Detach any child that pointed at the removed tab so it falls back to top level.
  navState.forEach(t => { if (t.parent === key) t.parent = ''; });
  renderNavEditor();
}

// Build the nav settings payload shared by the per-section and full saves.
function navPayload() {
  const customTabs = navState.filter(t => t.custom).map(t => ({
    id: t.key, label: t.label, icon: t.icon, url: t.url || '',
    parent: t.parent || '', heading: !!t.heading,
  }));
  // Built-in nesting goes in nav_parents (custom tabs carry parent inline).
  const navParents = {};
  navState.filter(t => !t.custom && t.parent).forEach(t => { navParents[t.key] = t.parent; });
  return {
    nav_order:       navState.map(t => t.key).join(','),
    nav_hidden:      navState.filter(t => t.hidden).map(t => t.key).join(','),
    custom_nav_tabs: customTabs,
    nav_parents:     navParents,
  };
}

// Theme
async function applyTheme(value) {
  const hint = document.getElementById('theme-hint');
  if (hint) hint.textContent = 'Saving…';
  try {
    await fetch('setup/theme', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ui_theme: value}),
    });
    location.reload();
  } catch (e) {
    if (hint) hint.textContent = 'Save failed. Try Save All.';
  }
}

// Attached display (scale + orientation)
async function applyDisplay() {
  const hint = document.getElementById('display-hint');
  if (hint) hint.textContent = 'Saving…';
  const ui_scale = document.getElementById('ui_scale')?.value || 'normal';
  const display_rotation = parseInt(document.getElementById('display_rotation')?.value || '0', 10);
  try {
    await fetch('setup/scale', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ui_scale, display_rotation}),
    });
    // Restart the kiosk browser so the attached display picks up the new scale
    // and orientation without a reboot, even when this change is made from a
    // separate phone or laptop (FoodAssistant-1njb). Best-effort and Pi-only.
    fetch('setup/kiosk/restart', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).catch(() => {});
    location.reload();
  } catch (e) {
    if (hint) hint.textContent = 'Save failed, try Save All.';
  }
}
