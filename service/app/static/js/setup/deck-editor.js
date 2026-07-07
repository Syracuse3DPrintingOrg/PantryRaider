// Start Page & Stream Deck sub-areas: the pane toggle switches between the
// on-screen Start Page (#pane-start-page, the pill's own target) and the
// physical Stream Deck editor (#pane-streamdeck). The deck pane has no pill of
// its own, so its active classes are flipped manually.
function showDeckStart(which) {
  const s = document.getElementById('pane-start-page');
  const d = document.getElementById('pane-streamdeck');
  if (which === 'deck' && d) {
    // Bring any custom keys built on the Start Page across to the deck before
    // showing it, so a key made in one editor appears in the other with no reload.
    _deckStartSyncDefs('start-overrides', 'streamdeck-overrides', _sdOverrideRow);
    if (s) s.classList.remove('active', 'show');
    d.classList.add('active', 'show');
    _sdRefreshCustom();
  } else {
    which = 'start';
    if (d) d.classList.remove('active', 'show');
    if (s) s.classList.add('active', 'show');
    _startEditorInit();
    // ...and the same the other way: deck-built custom keys show on the Start Page.
    _deckStartSyncDefs('streamdeck-overrides', 'start-overrides', _startBuildRow);
    _startRenderPalette();
    _startEditorRender();
  }
  document.querySelectorAll('.ds-toggle [data-ds]').forEach(b =>
    b.classList.toggle('active', b.dataset.ds === which));
}

// Custom keys are one shared library across the two editors, but each editor
// keeps its own set of builder rows. When the user switches editors, merge the
// rows from the editor being left into the one being entered (the editor just
// used wins on a shared id). No-op when a container is absent (off-Pi there is
// only the Start Page). Grid placements are keyed by id, so rebuilding the rows
// leaves any placements intact.
function _deckStartSyncDefs(fromWrapId, toWrapId, toBuilder) {
  const from = document.getElementById(fromWrapId);
  const to = document.getElementById(toWrapId);
  if (!from || !to) return;
  const merged = new Map();
  for (const row of to.querySelectorAll('.sd-override-row')) {
    const def = _sdDefFromRow(row); if (def) merged.set(def.id, def);
  }
  for (const row of from.querySelectorAll('.sd-override-row')) {
    const def = _sdDefFromRow(row); if (def) merged.set(def.id, def);
  }
  to.innerHTML = '';
  for (const def of merged.values()) to.appendChild(toBuilder(def));
}

// -- On-screen Start Page editor (Pantry Raider) ---------------------------
// _startState is a flat token array (one per key): a built-in action key, a
// "custom:<id>" reference into the shared custom-button store, or "" for blank.
let _startState = null;
// Drag/selection state is shared with the Stream Deck editor via _sdDragSrc so
// both grids use the same cell engine.
const _START_SHAPES = { 6: [3, 2], 15: [5, 3], 32: [8, 4] };

function _startKeyCount() {
  return parseInt(document.getElementById('start_page_keys')?.value || '15', 10) || 15;
}
// Live custom-key definitions from the Start Page's own editor rows (shared
// builder with the deck via _sdOverrideRow / _sdDefFromRow).
function _startCustomDefs() {
  const wrap = document.getElementById('start-overrides');
  if (!wrap) return [];
  const out = [];
  for (const row of wrap.querySelectorAll('.sd-override-row')) {
    const def = _sdDefFromRow(row);
    if (def) out.push(def);
  }
  return out;
}
function _startCustomMeta(def) {
  // Reuse the deck's face metadata (label/icon) so a custom key looks identical
  // on the Start Page and the Stream Deck.
  try { return _sdDefMeta(def); } catch (e) { return { label: def.label || 'Custom', icon: def.icon || 'bi-grid-1x2' }; }
}
// Build a Start Page custom-key editor row from the deck's shared row builder,
// rewiring its delete + change handlers to refresh the Start Page palette.
function _startBuildRow(o) {
  const row = _sdOverrideRow(o || {});
  const del = row.querySelector('.btn-outline-danger');
  if (del) del.onclick = function () { row.remove(); _startRenderPalette(); _startEditorRender(); };
  row.addEventListener('input', () => { _startRenderPalette(); _startEditorRender(); });
  row.addEventListener('change', () => { _startRenderPalette(); _startEditorRender(); });
  return row;
}
function _startAddOverrideRow() {
  const wrap = document.getElementById('start-overrides');
  if (!wrap) return;
  wrap.appendChild(_startBuildRow({}));
  _startRenderPalette();
}
async function _startEditorInit() {
  // Load the deck's own action catalog so the Start Page palette and keys are
  // identical to the Stream Deck (same source, on a Pi it is the host bridge).
  try { await _sdLoadCatalog(); } catch (e) { }
  if (_startState) { _startEditorRender(); return; }   // keep edits across pane switches
  const n = _startKeyCount();
  _startState = [];
  const saved = window.START_LAYOUT || [];
  for (let i = 0; i < n; i++) _startState.push(saved[i] || '');
  // Populate the shared custom-key library into the Start Page's own editor.
  const wrap = document.getElementById('start-overrides');
  if (wrap && !wrap.dataset.loaded) {
    const seen = new Set();
    for (const o of (window.START_OVERRIDES || [])) {
      if (!o || !_sdIsCustomId(o.id) || seen.has(o.id)) continue;
      seen.add(o.id);
      wrap.appendChild(_startBuildRow(o));
    }
    wrap.dataset.loaded = '1';
  }
  _startRenderPalette();
  _startEditorRender();
}
function _startRenderPalette() {
  const wrap = document.getElementById('start-palette');
  if (!wrap) return;
  wrap.innerHTML = '';
  // Coloured draggable chips + group rows, identical in style to the Stream
  // Deck palette so the two editors look the same.
  const makeChip = (tok, label, color, iconCls) => {
    const chip = document.createElement('div');
    chip.style.cssText = [
      'background:' + (color || '#374151'), 'color:#fff', 'border-radius:4px',
      'padding:2px 8px', 'font-size:0.7rem', 'cursor:grab', 'user-select:none',
      'border:1px solid rgba(255,255,255,0.2)',
    ].join(';');
    chip.draggable = true; chip.dataset.tok = tok;
    chip.innerHTML = (iconCls ? '<i class="' + iconCls + ' me-1"></i>' : '') + escapeHtml(label);
    chip.addEventListener('dragstart', e => { _sdDragSrc = {type:'palette', name: tok}; e.dataTransfer.effectAllowed = 'copy'; chip.style.opacity = '0.5'; });
    chip.addEventListener('dragend', () => { chip.style.opacity = '1'; });
    chip.addEventListener('click', () => { _sdDragSrc = {type:'palette', name: tok}; chip.style.outline = '2px solid #0dcaf0'; setTimeout(() => chip.style.outline = '', 600); });
    return chip;
  };
  const makeRow = (gname) => {
    const row = document.createElement('div');
    row.className = 'd-flex align-items-center gap-1 flex-wrap';
    const lbl = document.createElement('span');
    lbl.className = 'text-secondary small';
    lbl.style.cssText = 'min-width:90px;font-size:0.65rem;';
    lbl.textContent = gname + ':';
    row.appendChild(lbl);
    return row;
  };
  // Custom keys first (shared with the deck), then the deck's own action catalog
  // grouped exactly like the Stream Deck palette, so the two are identical.
  const customDefs = _startCustomDefs();
  if (customDefs.length) {
    const row = makeRow('Custom');
    customDefs.forEach(def => { const m = _startCustomMeta(def); const ic = m.icon ? 'bi bi-' + String(m.icon).replace(/^bi-/, '') : ''; row.appendChild(makeChip(def.id, m.label, m.color || '#374151', ic)); });
    wrap.appendChild(row);
  }
  const groups = {};
  (Array.isArray(_sdCatalog) ? _sdCatalog : []).forEach(a => { (groups[a.group || 'Other'] = groups[a.group || 'Other'] || []).push(a); });
  Object.keys(groups).forEach(gname => {
    const row = makeRow(gname);
    groups[gname].forEach(a => row.appendChild(makeChip(a.name, a.label, a.color, _sdIconClass(a))));
    wrap.appendChild(row);
  });
}
function _startEditorRender() {
  const grid = document.getElementById('start-grid');
  if (!grid) return;
  const n = _startKeyCount();
  if (!_startState) { _startEditorInit(); return; }
  // Resize the token array to the chosen key count (truncate or pad with blanks).
  if (_startState.length > n) _startState = _startState.slice(0, n);
  while (_startState.length < n) _startState.push('');
  const [cols] = _START_SHAPES[n] || [5];
  // Match the Stream Deck grid: same column sizing and coloured square keys.
  grid.style.gridTemplateColumns = 'repeat(' + cols + ', minmax(0,1fr))';
  grid.innerHTML = '';
  // Same cell engine as the Stream Deck editor. ctx resolves custom keys from the
  // Start Page's own override rows, allows click-to-place/clear, and omits the
  // deck position badge. Rebuilt each render since _startState may be reassigned.
  const startDefById = id => _startCustomDefs().find(d => d.id === id) || null;
  const ctx = {
    keys: _startState, refresh: _startEditorRender, badge: false, click: true,
    defById: startDefById,
  };
  _startState.forEach((tok, i) => grid.appendChild(_sdMakeCell(ctx, i, tok || 'blank', i)));
}
function savePaneStartPage(btn) {
  if (!_startState) _startEditorInit();
  return savePane({
    start_page_enabled: chk('start_page_enabled'),
    start_page_keys:    _startKeyCount(),
    start_page_layout:  _startState.slice(),
    // The custom-key definitions are merged into the shared store (preserving
    // any Stream Deck slot placements) by the server, so a key built here also
    // shows up on the deck. start_loaded_ids lets the server tell a key the user
    // removed here from one added on the deck since this page loaded.
    start_custom_defs:  _startCustomDefs(),
    start_loaded_ids:   (window.START_OVERRIDES || []).map(o => o && o.id).filter(Boolean),
    // Key style + icon treatment are shared with the deck (same settings).
    streamdeck_key_style:  optVal('start_key_style'),
    streamdeck_icon_color: optVal('start_icon_color'),
    // Enabling/disabling the Start Page adds or removes the Start nav tab and
    // changes the home page, so reload so the interface reflects it at once.
  }, btn, 'start-page-result').then(() => { setTimeout(() => location.reload(), 400); });
}

// Stream Deck visual key-grid editor
const SD_GRID = { 6: {cols:3, rows:2}, 15: {cols:5, rows:3}, 32: {cols:8, rows:4} };
let _sdCatalog = null;       // [{name,label,kind,group,color,description}]
let _sdKeys = [];            // flat, deck-index order; "blank" for empty
let _sdPage = 0;             // page being edited (the controller paginates _sdKeys)
let _sdDragSrc = null;       // {type:'grid'|'palette', idx?:number, name:string}

async function _sdLoadCatalog() {
  if (_sdCatalog) return _sdCatalog;
  try {
    const r = await fetch('setup/streamdeck/actions').then(x => x.json());
    if (r.ok && Array.isArray(r.actions)) { _sdCatalog = r.actions; return _sdCatalog; }
  } catch (e) { /* fall through to fallback */ }
  // Last-resort list so the editor still renders if the fetch itself fails.
  // The server endpoint already falls back to the bundled generated catalog
  // (service/app/data/deck_catalog.json) off-Pi, so this rarely runs.
  _sdCatalog = [
    {name:'blank',label:'Empty',kind:'blank',group:'System',color:'#1f2937'},
    {name:'expiring',label:'Expiring',kind:'status',group:'Status',color:'#b54708'},
    {name:'pending',label:'Pending',kind:'status',group:'Status',color:'#1d4ed8'},
    {name:'commit',label:'Commit',kind:'trigger',group:'Actions',color:'#15803d'},
    {name:'add',label:'Pantry',kind:'nav',group:'Navigation',color:'#b45309'},
    {name:'inventory',label:'Stock',kind:'nav',group:'Navigation',color:'#0f766e'},
    {name:'cook',label:'Cook',kind:'nav',group:'Navigation',color:'#7e22ce'},
    {name:'recipes',label:'Recipes',kind:'nav',group:'Navigation',color:'#7e22ce'},
    {name:'mealplan',label:'Plan',kind:'nav',group:'Navigation',color:'#7e22ce'},
    {name:'shopping',label:'Shop',kind:'nav',group:'Navigation',color:'#7e22ce'},
    {name:'defaults',label:'Defaults',kind:'nav',group:'Navigation',color:'#7e22ce'},
    {name:'timer_1',label:'Timer 1',kind:'timer',group:'Timers',color:'#0d9488'},
    {name:'timer_2',label:'Timer 2',kind:'timer',group:'Timers',color:'#0d9488'},
    {name:'timer_3',label:'Timer 3',kind:'timer',group:'Timers',color:'#0d9488'},
    {name:'weather',label:'Weather',kind:'weather',group:'Weather',color:'#1e40af'},
    {name:'forecast',label:'Forecast',kind:'forecast',group:'Weather',color:'#0e7490'},
    {name:'ha_1',label:'HA 1',kind:'ha_entity',group:'Home Assistant',color:'#475569'},
    {name:'ha_2',label:'HA 2',kind:'ha_entity',group:'Home Assistant',color:'#475569'},
    {name:'ha_3',label:'HA 3',kind:'ha_entity',group:'Home Assistant',color:'#475569'},
    {name:'ha_4',label:'HA 4',kind:'ha_entity',group:'Home Assistant',color:'#475569'},
    {name:'ha_5',label:'HA 5',kind:'ha_entity',group:'Home Assistant',color:'#475569'},
    {name:'brightness',label:'Bright',kind:'system',group:'System',color:'#475569'},
    {name:'page_next',label:'More',kind:'system',group:'System',color:'#334155'},
    {name:'page_prev',label:'Back',kind:'system',group:'System',color:'#334155'},
  ];
  return _sdCatalog;
}

function _sdActionByName(name) {
  if (!_sdCatalog) return null;
  return _sdCatalog.find(a => a.name === name) || null;
}

// Bootstrap Icons class for a catalog action's glyph (the deck uses the same
// glyph names). Accepts "plus-circle" or "bi-plus-circle"; '' when unset.
function _sdIconClass(action) {
  const g = action && action.icon ? String(action.icon).replace(/^bi-/, '') : '';
  return g ? 'bi bi-' + g : '';
}

function _sdEffectiveDim(count, rotation) {
  const dim = SD_GRID[count] || SD_GRID[15];
  // For 90 or 270 degree rotation the deck is mounted on its side,
  // so the visible grid swaps columns and rows.
  if (rotation === 90 || rotation === 270) return {cols: dim.rows, rows: dim.cols};
  return dim;
}

// Shared grid-cell renderer for BOTH the Stream Deck editor and the on-screen
// Start Page editor, so a key looks and behaves identically in each. `ctx`
// carries the per-editor differences:
//   ctx.keys    - the flat token array this editor mutates (_sdKeys / _startState)
//   ctx.refresh - re-render callback for that editor
//   ctx.badge   - show the 1-based deck position in the corner (deck only)
//   ctx.click   - allow click-to-place / click-to-clear (Start Page only)
// The larger, roomier key style from the Start Page is used for both.
function _sdMakeCell(ctx, idx, name, displayPos) {
  // idx is the GLOBAL index into ctx.keys (so drag/drop/swap operate on the full
  // paginated list); displayPos is the 1-based key position on the physical deck
  // page shown in the corner badge. Defaults to idx when not paginating.
  if (displayPos === undefined) displayPos = idx;
  // A custom-key id renders from its override definition; everything else is a
  // built-in catalog action. A custom id whose definition was deleted falls
  // back to an empty cell.
  let face, custom = false, iconName = '';
  // Custom-key definitions live in a per-editor override container (the deck's
  // vs the Start Page's), so the lookup is supplied by ctx.
  const defById = ctx.defById || _sdCustomDefById;
  if (_sdIsCustomId(name)) {
    const def = defById(name);
    if (def) {
      const meta = _sdDefMeta(def);
      face = {label: meta.label, color: meta.color};
      iconName = meta.icon;
      custom = true;
    } else {
      face = {name: 'blank', label: 'Empty', color: '#1f2937'};
    }
  } else {
    face = _sdActionByName(name) || {name: 'blank', label: 'Empty', color: '#1f2937'};
  }
  const blank = !custom && face.name === 'blank';
  const cell = document.createElement('div');
  cell.className = 'rounded position-relative';
  cell.style.cssText = [
    'aspect-ratio:1', 'min-width:74px', 'min-height:74px', 'display:flex',
    'flex-direction:column', 'align-items:center', 'justify-content:center',
    'gap:4px', 'overflow:hidden', `background:${face.color || '#1f2937'}`,
    'cursor:' + (blank && ctx.click ? 'pointer' : 'grab'), 'user-select:none',
    'transition:filter 0.1s', 'border-radius:0.75rem', 'border:2px solid rgba(255,255,255,0.15)',
    (blank ? 'opacity:0.55;border-style:dashed' : ''),
  ].join(';');
  // On the Start Page a blank cell is a click target, not a drag source; on the
  // deck every cell drags (so empty slots can be swapped/moved).
  cell.draggable = ctx.click ? !blank : true;
  cell.dataset.idx = String(idx);

  if (blank && ctx.click) {
    const plus = document.createElement('i');
    plus.className = 'bi bi-plus';
    plus.style.cssText = 'color:#fff;opacity:0.6;font-size:1.7rem;line-height:1;';
    cell.appendChild(plus);
  } else {
    const iconCls = custom
      ? (iconName ? 'bi bi-' + String(iconName).replace(/^bi-/, '') : '')
      : _sdIconClass(face);
    if (iconCls) {
      const ic = document.createElement('i');
      ic.className = iconCls;
      ic.style.cssText = 'color:#fff;font-size:1.6rem;line-height:1;';
      cell.appendChild(ic);
    }
    const lbl = document.createElement('span');
    lbl.style.cssText = 'color:#fff;font-size:0.72rem;line-height:1.05;text-align:center;word-break:break-word;padding:0 4px;';
    lbl.textContent = face.label;
    cell.appendChild(lbl);
  }

  if (custom) {
    const mark = document.createElement('span');
    mark.style.cssText = 'position:absolute;top:3px;left:4px;font-size:0.55rem;color:rgba(255,255,255,0.85);';
    mark.title = 'Custom key';
    mark.innerHTML = '<i class="bi bi-pencil-fill"></i>';
    cell.appendChild(mark);
  }

  if (ctx.badge) {
    const badge = document.createElement('span');
    badge.style.cssText = 'position:absolute;top:3px;right:4px;font-size:0.55rem;color:rgba(255,255,255,0.45);';
    badge.textContent = String(displayPos + 1);
    cell.appendChild(badge);
  }

  cell.title = ctx.click
    ? (blank ? 'Drop or click to place an action here' : (face.label + ' (click to clear)'))
    : '';

  if (ctx.click) {
    cell.addEventListener('click', () => {
      // A selected palette chip (via click) drops into this cell; otherwise a
      // click on a filled cell clears it.
      if (_sdDragSrc && _sdDragSrc.type === 'palette') { ctx.keys[idx] = _sdDragSrc.name; _sdDragSrc = null; }
      else if (!blank) { ctx.keys[idx] = ''; }
      ctx.refresh();
    });
  }

  cell.addEventListener('dragstart', e => {
    _sdDragSrc = {type:'grid', idx, name: ctx.keys[idx] || 'blank'};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', ctx.keys[idx] || 'blank');
    cell.style.opacity = '0.45';
  });
  cell.addEventListener('dragend', () => { cell.style.opacity = '1'; });
  cell.addEventListener('dragover', e => {
    e.preventDefault();
    cell.style.filter = 'brightness(1.5)';
    cell.style.borderColor = 'rgba(255,255,255,0.7)';
    e.dataTransfer.dropEffect = _sdDragSrc?.type === 'grid' ? 'move' : 'copy';
  });
  cell.addEventListener('dragleave', () => {
    cell.style.filter = '';
    cell.style.borderColor = 'rgba(255,255,255,0.15)';
  });
  cell.addEventListener('drop', e => {
    e.preventDefault();
    cell.style.filter = '';
    cell.style.borderColor = 'rgba(255,255,255,0.15)';
    if (!_sdDragSrc) return;
    const tgtIdx = parseInt(cell.dataset.idx, 10);
    if (_sdDragSrc.type === 'grid') {
      const tmp = ctx.keys[tgtIdx] || 'blank';
      ctx.keys[tgtIdx] = ctx.keys[_sdDragSrc.idx] || 'blank';
      ctx.keys[_sdDragSrc.idx] = tmp;
    } else {
      ctx.keys[tgtIdx] = _sdDragSrc.name;
    }
    _sdDragSrc = null;
    ctx.refresh();
  });

  return cell;
}

// Pagination math mirroring layout.build_pages: a flat keys list that fits a
// deck stays one page; otherwise each page reserves its last key for "More" and
// holds `count - 1` editable keys. Trailing blanks do not count toward length.
function _sdPageInfo(count) {
  let len = _sdKeys.length;
  while (len > 0 && (_sdKeys[len - 1] === 'blank' || !_sdKeys[len - 1])) len--;
  if (len <= count) return {usable: count, npages: 1, paginated: false};
  const usable = count - 1;
  return {usable, npages: Math.ceil(len / usable), paginated: true};
}

// The reserved auto "More" key on a paginated page: visible, labelled, but not a
// drag/drop target (the controller manages it). Clicking it flips the editor to
// the next page, like pressing it on the deck.
function _sdMakeMoreCell(displayPos) {
  const cell = document.createElement('div');
  cell.className = 'rounded position-relative';
  cell.style.cssText = [
    'aspect-ratio:1', 'min-width:74px', 'min-height:74px', 'display:flex', 'flex-direction:column',
    'align-items:center', 'justify-content:center', 'gap:4px', 'overflow:hidden',
    'background:#334155', 'cursor:pointer', 'user-select:none',
    'border-radius:0.75rem', 'border:2px dashed rgba(255,255,255,0.35)',
  ].join(';');
  cell.title = 'Auto "More" key (cycles pages on the deck)';
  cell.innerHTML =
    '<i class="bi bi-arrow-repeat" style="color:#fff;font-size:1.6rem;line-height:1;"></i>' +
    '<span style="color:#fff;font-size:0.72rem;line-height:1.05;">More</span>' +
    '<span style="position:absolute;top:3px;right:4px;font-size:0.55rem;color:rgba(255,255,255,0.45);">' +
    String(displayPos + 1) + '</span>';
  cell.addEventListener('click', () => _sdGotoPage(1));
  return cell;
}

function _sdRenderPageNav(info) {
  const nav = document.getElementById('streamdeck-page-nav');
  if (!nav) return;
  if (!info.paginated && info.npages <= 1) {
    // Single page: only offer "Add page" so the user can start a second page.
    nav.classList.remove('d-none');
    nav.innerHTML = '';
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'btn btn-outline-secondary btn-sm';
    add.innerHTML = '<i class="bi bi-plus-lg me-1"></i>Add page';
    add.onclick = _sdAddPage;
    nav.appendChild(add);
    return;
  }
  nav.classList.remove('d-none');
  nav.innerHTML =
    '<button type="button" class="btn btn-outline-secondary btn-sm" ' +
    (_sdPage <= 0 ? 'disabled' : '') + ' onclick="_sdGotoPage(-1)"><i class="bi bi-chevron-left"></i></button>' +
    '<span class="small">Page ' + (_sdPage + 1) + ' of ' + info.npages + '</span>' +
    '<button type="button" class="btn btn-outline-secondary btn-sm" ' +
    (_sdPage >= info.npages - 1 ? 'disabled' : '') + ' onclick="_sdGotoPage(1)"><i class="bi bi-chevron-right"></i></button>' +
    '<button type="button" class="btn btn-outline-secondary btn-sm ms-2" onclick="_sdAddPage()"><i class="bi bi-plus-lg me-1"></i>Add page</button>';
}

function _sdGotoPage(delta) {
  const total = (() => {
    const count = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
    const rotation = parseInt(document.getElementById('streamdeck_rotation')?.value || '0', 10);
    const dim = _sdEffectiveDim(count, rotation);
    return dim.cols * dim.rows;
  })();
  const info = _sdPageInfo(total);
  _sdPage = Math.max(0, Math.min(info.npages - 1, _sdPage + delta));
  _sdRenderGrid();
}

// Append a page of empty slots so the user can place keys on a new page. Going
// from one page to two converts the layout to paginated (the previous last key
// becomes the first page's "More" neighbour), exactly as the deck would.
function _sdAddPage() {
  const count = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
  const rotation = parseInt(document.getElementById('streamdeck_rotation')?.value || '0', 10);
  const dim = _sdEffectiveDim(count, rotation);
  const total = dim.cols * dim.rows;
  const usable = total - 1;
  // Pad so at least one more full editable page exists beyond what is used now.
  let len = _sdKeys.length;
  while (len > 0 && (_sdKeys[len - 1] === 'blank' || !_sdKeys[len - 1])) len--;
  const curPages = len <= total ? 1 : Math.ceil(len / usable);
  const target = (curPages + 1) * usable;
  while (_sdKeys.length < target) _sdKeys.push('blank');
  _sdPage = curPages;   // jump to the new page
  _sdRenderGrid();
}

// Stock key order, mirroring foodassistant_streamdeck.actions.DEFAULT_ORDER.
// Used by the "Reset layout to default" button (FoodAssistant-o7cz). Keep in
// step with actions.py; an extra/missing name is harmless (unknowns are dropped).
const SD_DEFAULT_ORDER = [
  'expiring', 'pending', 'ready', 'shopping_count', 'commit', 'add', 'inventory',
  'cook', 'recipes', 'mealplan', 'shopping', 'meal_today', 'cooked', 'timer_1',
  'timer_2', 'timer_3', 'timer_eggs', 'timer_pasta', 'timer_rice', 'timers_view',
  'convert', 'clock', 'weather', 'forecast', 'health', 'camera', 'screen_off', 'brightness',
];

async function _sdResetLayout() {
  if (!confirm('Reset the key layout to the default arrangement? Your custom keys are kept in the palette.')) return;
  _sdKeys = SD_DEFAULT_ORDER.slice();
  _sdPage = 0;
  await _sdRenderGrid();
}

async function _sdRenderGrid() {
  await _sdLoadCatalog();
  const grid = document.getElementById('streamdeck-grid');
  const palette = document.getElementById('streamdeck-palette');
  if (!grid) return;

  const count = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
  const rotation = parseInt(document.getElementById('streamdeck_rotation')?.value || '0', 10);
  const dim = _sdEffectiveDim(count, rotation);

  grid.style.gridTemplateColumns = `repeat(${dim.cols}, minmax(0,1fr))`;

  // The controller paginates _sdKeys across deck pages: when more keys are used
  // than fit, the LAST key of each page becomes an auto "More" key and the rest
  // continue on the next page. Mirror that here so the editor can author every
  // page. `usable` is the editable keys per page (one less when paginated, to
  // leave room for the More key); `total` is the grid cell count.
  const total = dim.cols * dim.rows;
  const info = _sdPageInfo(total);
  if (_sdPage >= info.npages) _sdPage = info.npages - 1;
  if (_sdPage < 0) _sdPage = 0;
  const pageStart = info.paginated ? _sdPage * info.usable : 0;
  // Make sure every editable slot on this page exists in _sdKeys.
  while (_sdKeys.length < pageStart + info.usable) _sdKeys.push('blank');

  grid.innerHTML = '';
  // ctx must be rebuilt each render because _sdKeys can be reassigned; the drop
  // handler mutates ctx.keys in place by index, which stays valid within a render.
  const ctx = {keys: _sdKeys, refresh: _sdRefreshCustom, badge: true, click: false};
  for (let pos = 0; pos < total; pos++) {
    if (info.paginated && pos === total - 1) {
      // Reserved auto "More" key (page cycle); shown but not editable.
      grid.appendChild(_sdMakeMoreCell(pos));
    } else {
      const globalIdx = pageStart + pos;
      grid.appendChild(_sdMakeCell(ctx, globalIdx, _sdKeys[globalIdx] || 'blank', pos));
    }
  }
  _sdRenderPageNav(info);

  // Render grouped palette below the grid. A draggable chip is added per built-in
  // action, plus a "Custom" group with one chip per user-defined custom key, so
  // a custom key is dropped onto the grid exactly like a built-in action.
  if (palette) {
    palette.innerHTML = '';
    const makeChip = (label, color, iconCls, dragName, title) => {
      const chip = document.createElement('div');
      chip.style.cssText = [
        `background:${color || '#374151'}`, 'color:#fff', 'border-radius:4px',
        'padding:2px 8px', 'font-size:0.7rem', 'cursor:grab', 'user-select:none',
        'border:1px solid rgba(255,255,255,0.2)',
      ].join(';');
      chip.draggable = true;
      chip.innerHTML = (iconCls ? `<i class="${iconCls} me-1"></i>` : '') + escapeHtml(label);
      if (title) chip.title = title;
      chip.addEventListener('dragstart', e => {
        _sdDragSrc = {type: 'palette', name: dragName};
        e.dataTransfer.effectAllowed = 'copy';
        e.dataTransfer.setData('text/plain', dragName);
        chip.style.opacity = '0.5';
      });
      chip.addEventListener('dragend', () => { chip.style.opacity = '1'; });
      return chip;
    };
    const makeRow = (gname) => {
      const row = document.createElement('div');
      row.className = 'd-flex align-items-center gap-1 flex-wrap';
      const lbl = document.createElement('span');
      lbl.className = 'text-secondary small';
      lbl.style.cssText = 'min-width:90px;font-size:0.65rem;';
      lbl.textContent = gname + ':';
      row.appendChild(lbl);
      return row;
    };

    // Custom keys first, so they are easy to find right under the grid.
    const customDefs = _sdCustomDefs();
    if (customDefs.length) {
      const row = makeRow('Custom');
      for (const def of customDefs) {
        const meta = _sdDefMeta(def);
        const iconCls = meta.icon ? 'bi bi-' + String(meta.icon).replace(/^bi-/, '') : '';
        row.appendChild(makeChip(meta.label, meta.color, iconCls, def.id, 'Custom key (drag onto a grid cell)'));
      }
      palette.appendChild(row);
    }

    const groups = {};
    for (const a of _sdCatalog) {
      (groups[a.group] = groups[a.group] || []).push(a);
    }
    for (const [gname, items] of Object.entries(groups)) {
      const row = makeRow(gname);
      for (const a of items) {
        row.appendChild(makeChip(a.label, a.color, _sdIconClass(a), a.name, a.description || ''));
      }
      palette.appendChild(row);
    }
  }
}

function _sdCollectKeys() {
  // A custom key occupies its grid slot, but the deck's "keys" list only holds
  // built-in action names (it drops unknown ones, which would shift the layout).
  // So a custom slot is saved as "blank"; its slot-based override fills it.
  const keys = _sdKeys.map(n => (_sdIsCustomId(n) ? 'blank' : n));
  // Trim trailing blanks so we do not persist a full row of empties at the end.
  while (keys.length && keys[keys.length - 1] === 'blank') keys.pop();
  return keys;
}

// Default tile colours per override type, mirroring _OVERRIDE_DEFAULT_COLORS
// and _OVERRIDE_DEFAULT_ICONS in streamdeck/actions.py so the grid preview
// matches what the deck will actually render.
const _SD_OVERRIDE_PREVIEW = {
  ha_action: {color: '#475569', icon: 'house', label: 'HA'},
  timer: {color: '#0d9488', icon: 'stopwatch', label: 'Timer'},
  weather: {color: '#1e40af', icon: 'cloud-sun', label: 'Weather'},
  shopping_add: {color: '#0f766e', icon: 'cart-plus', label: 'Add'},
  macro: {color: '#6d28d9', icon: 'collection-play', label: 'Macro'},
  camera: {color: '#0f172a', icon: 'camera-video', label: 'Camera'},
  media: {color: '#7c3aed', icon: 'play-circle', label: 'Media'},
};

// Media transport actions a "media" override can bind to (mirrors MEDIA_ACTIONS
// in streamdeck/actions.py): value -> {label, icon} for the grid preview.
const _SD_MEDIA_ACTIONS = {
  play_pause: {label: 'Play/Pause', icon: 'play-circle'},
  next: {label: 'Next', icon: 'skip-forward'},
  previous: {label: 'Previous', icon: 'skip-backward'},
  volume_up: {label: 'Volume +', icon: 'volume-up'},
  volume_down: {label: 'Volume -', icon: 'volume-down'},
  stop: {label: 'Stop', icon: 'stop-circle'},
};

// The face a custom key (override definition) should show on the grid and the
// palette: {color, icon, label}, mirroring how the deck renders each type.
function _sdDefMeta(def) {
  const preset = _SD_OVERRIDE_PREVIEW[def.type] || {color: '#374151', icon: '', label: 'Custom'};
  let color = preset.color;
  let icon = def.icon || preset.icon;
  let label = def.label || preset.label;
  if (def.type === 'ha_action' && def.color_off) color = def.color_off;
  if (def.type === 'weather' && def.forecast) { color = '#0e7490'; icon = icon || 'thermometer-half'; if (!def.label) label = 'Forecast'; }
  if (def.type === 'shopping_add' && !def.label && def.item) label = def.item;
  if (def.type === 'macro' && !def.label && Array.isArray(def.actions) && def.actions.length) label = def.actions.length + ' steps';
  if (def.type === 'camera') { if (def.full) icon = icon || 'camera'; if (!def.label) label = def.camera || (def.full ? 'Full' : 'Camera'); }
  if (def.type === 'media') { const m = _SD_MEDIA_ACTIONS[def.action] || _SD_MEDIA_ACTIONS.play_pause; icon = def.icon || m.icon; if (!def.label) label = m.label; }
  return {color, icon, label};
}

// Advanced per-key override rows. Each row is a small inline editor whose
// type-specific fields show or hide as the type dropdown changes.
const _SD_OVERRIDE_TYPES = [
  {value: 'ha_action', label: 'Home Assistant action'},
  {value: 'timer', label: 'Countdown timer'},
  {value: 'weather', label: 'Weather tile'},
  {value: 'shopping_add', label: 'Quick-add shopping item'},
  {value: 'macro', label: 'Macro (run actions in sequence)'},
  {value: 'camera', label: 'Camera feed'},
  {value: 'media', label: 'Media control (Home Assistant)'},
];

// Curated Bootstrap Icons glyphs (names without the "bi-" prefix) offered in
// the HA action icon picker. Hand-picked to cover the common smart-home keys;
// a free-text entry remains available for anything not listed.
const _SD_HA_ICONS = [
  'house', 'lightbulb', 'toggle-on', 'power', 'plug', 'fan', 'thermometer-half',
  'door-open', 'lock', 'tv', 'speaker', 'music-note-beamed', 'bell',
  'fire', 'snow', 'droplet', 'shield-lock', 'star',
];

let _sdCidSeq = 0;

// True when a key name refers to a user-defined custom key (override) rather
// than a built-in action. Custom ids are "c1", "c2", ... so they never collide
// with catalog action names.
function _sdIsCustomId(name) {
  return typeof name === 'string' && /^c\d+$/.test(name);
}

function _sdOverrideRow(o) {
  o = o || {};
  if (!o.id || !_sdIsCustomId(o.id)) {
    o.id = 'c' + (++_sdCidSeq);
  } else {
    // Keep the sequence ahead of any loaded id so a new key never collides.
    const n = parseInt(o.id.slice(1), 10);
    if (Number.isFinite(n) && n > _sdCidSeq) _sdCidSeq = n;
  }
  const row = document.createElement('div');
  row.className = 'border rounded p-2 sd-override-row';
  row.dataset.cid = o.id;
  const typeOpts = _SD_OVERRIDE_TYPES.map(t =>
    `<option value="${t.value}"${o.type === t.value ? ' selected' : ''}>${escapeHtml(t.label)}</option>`
  ).join('');
  // Unique ids so multiple rows do not collide on datalist/checkbox ids.
  const uid = 'sdov' + (_sdOverrideRow._seq = (_sdOverrideRow._seq || 0) + 1);
  const iconListId = uid + '-icons';
  const forecastId = uid + '-fc';
  const macroListId = uid + '-macro';
  const camListId = uid + '-cam';
  const camFullId = uid + '-camfull';
  // Camera-name suggestions from the currently configured cameras, so a camera
  // override can pick one by name (free text still works for a not-yet-saved one).
  const camOpts = _sdCollectCameras()
    .map(c => c.name).filter(Boolean)
    .map(n => `<option value="${escapeHtml(n)}">`).join('');
  const iconOpts = _SD_HA_ICONS.map(g => `<option value="${escapeHtml(g)}">`).join('');
  // Offer the loaded action catalog (minus blank and macro keys, which a macro
  // cannot usefully run) as autocomplete suggestions for the macro field.
  const macroOpts = (Array.isArray(_sdCatalog) ? _sdCatalog : [])
    .filter(a => a.name !== 'blank' && a.kind !== 'macro')
    .map(a => `<option value="${escapeHtml(a.name)}">${escapeHtml(a.label || a.name)}</option>`)
    .join('');
  row.innerHTML = `
    <div class="row g-2 align-items-end">
      <div class="col-auto">
        <label class="form-label small mb-1">Type</label>
        <select class="form-select form-select-sm sd-ov-type">${typeOpts}</select>
      </div>
      <div class="col">
        <label class="form-label small mb-1">Label</label>
        <input type="text" class="form-control form-control-sm sd-ov-label" placeholder="Optional" value="${escapeHtml(o.label || '')}">
      </div>
      <div class="col-auto">
        <span class="sd-ov-placed badge bg-secondary" title="Drag this key from the palette below onto a grid cell">Not placed</span>
      </div>
      <div class="col-auto">
        <button type="button" class="btn btn-outline-danger btn-sm" title="Delete this custom key" onclick="this.closest('.sd-override-row').remove(); _sdRefreshCustom();">
          <i class="bi bi-trash"></i>
        </button>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-ha_action">
      <div class="col-md-6">
        <label class="form-label small mb-1">Entity ID</label>
        <input type="text" class="form-control form-control-sm sd-ov-entity" placeholder="light.kitchen" value="${escapeHtml(o.entity_id || '')}">
      </div>
      <div class="col-md-6">
        <label class="form-label small mb-1">Service (optional)</label>
        <input type="text" class="form-control form-control-sm sd-ov-service" placeholder="script.goodnight" value="${escapeHtml(o.service || '')}">
      </div>
      <div class="col-auto" style="max-width:130px">
        <label class="form-label small mb-1">On colour</label>
        <input type="color" class="form-control form-control-color form-control-sm sd-ov-color-on" value="${escapeHtml(o.color_on || '#15803d')}" title="Background when the entity is on">
      </div>
      <div class="col-auto" style="max-width:130px">
        <label class="form-label small mb-1">Off colour</label>
        <input type="color" class="form-control form-control-color form-control-sm sd-ov-color-off" value="${escapeHtml(o.color_off || '#475569')}" title="Background when the entity is off">
      </div>
      <div class="col">
        <label class="form-label small mb-1">Icon</label>
        <input type="text" class="form-control form-control-sm sd-ov-icon" list="${iconListId}" placeholder="house" value="${escapeHtml(o.icon || '')}">
        <datalist id="${iconListId}">${iconOpts}</datalist>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-timer">
      <div class="col-auto" style="max-width:160px">
        <label class="form-label small mb-1">Minutes</label>
        <input type="number" min="0" class="form-control form-control-sm sd-ov-minutes" value="${o.minutes != null ? o.minutes : 5}">
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-weather align-items-end">
      <div class="col-md-6">
        <label class="form-label small mb-1">Location</label>
        <input type="text" class="form-control form-control-sm sd-ov-location" placeholder="City, zip, or lat,lon (empty = auto)" value="${escapeHtml(o.location || '')}">
      </div>
      <div class="col-auto" style="max-width:140px">
        <label class="form-label small mb-1">Units</label>
        <select class="form-select form-select-sm sd-ov-weather-units">
          <option value=""${!o.units ? ' selected' : ''}>Global default</option>
          <option value="f"${o.units === 'f' ? ' selected' : ''}>Fahrenheit (°F)</option>
          <option value="c"${o.units === 'c' ? ' selected' : ''}>Celsius (°C)</option>
        </select>
      </div>
      <div class="col">
        <label class="form-label small mb-1">Icon (optional)</label>
        <input type="text" class="form-control form-control-sm sd-ov-weather-icon" list="${iconListId}" placeholder="cloud-sun" value="${escapeHtml(o.icon || '')}">
      </div>
      <div class="col-auto">
        <div class="form-check">
          <input class="form-check-input sd-ov-forecast" type="checkbox" id="${forecastId}"${o.forecast ? ' checked' : ''}>
          <label class="form-check-label small" for="${forecastId}">Show forecast (high/low) instead of current</label>
        </div>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-shopping_add">
      <div class="col-md-8">
        <label class="form-label small mb-1">Item</label>
        <input type="text" class="form-control form-control-sm sd-ov-item" placeholder="Milk" value="${escapeHtml(o.item || '')}">
        <div class="form-text">Pressing the key adds this product to the Mealie shopping list.</div>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-macro">
      <div class="col-12">
        <label class="form-label small mb-1">Actions</label>
        <input type="text" class="form-control form-control-sm sd-ov-actions" list="${macroListId}" placeholder="brightness, cook" value="${escapeHtml(Array.isArray(o.actions) ? o.actions.join(', ') : (o.actions || ''))}">
        <datalist id="${macroListId}">${macroOpts}</datalist>
        <div class="form-text">Comma-separated action names, run in order on a single press. Other macros are skipped.</div>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-camera">
      <div class="col-md-7">
        <label class="form-label small mb-1">Camera</label>
        <input type="text" class="form-control form-control-sm sd-ov-camera" list="${camListId}" placeholder="(first camera)" value="${escapeHtml(o.camera || o.camera_name || '')}">
        <datalist id="${camListId}">${camOpts}</datalist>
        <div class="form-text">Which configured camera to show. Leave blank for the first one. Configure cameras under Connections in the Settings menu.</div>
      </div>
      <div class="col-md-5 d-flex align-items-end">
        <div class="form-check">
          <input class="form-check-input sd-ov-camera-full" type="checkbox" id="${camFullId}"${o.full ? ' checked' : ''}>
          <label class="form-check-label small" for="${camFullId}">Full deck (press to take over every key)</label>
        </div>
      </div>
    </div>
    <div class="row g-2 mt-1 sd-ov-fields-media">
      <div class="col-md-6">
        <label class="form-label small mb-1">Media player entity</label>
        <input type="text" class="form-control form-control-sm sd-ov-media-entity" placeholder="media_player.living_room" value="${escapeHtml(o.entity_id || '')}">
      </div>
      <div class="col-md-6">
        <label class="form-label small mb-1">Control</label>
        <select class="form-select form-select-sm sd-ov-media-action">
          ${Object.entries(_SD_MEDIA_ACTIONS).map(([v, m]) => `<option value="${v}"${o.action === v ? ' selected' : ''}>${escapeHtml(m.label)}</option>`).join('')}
        </select>
        <div class="form-text">Calls the Home Assistant media_player service on press. Set the connection under Home Assistant.</div>
      </div>
    </div>`;
  const typeSel = row.querySelector('.sd-ov-type');
  const sync = () => {
    const t = typeSel.value;
    for (const tt of _SD_OVERRIDE_TYPES) {
      const block = row.querySelector('.sd-ov-fields-' + tt.value);
      if (block) block.classList.toggle('d-none', tt.value !== t);
    }
    _sdRefreshCustom();
  };
  typeSel.addEventListener('change', sync);
  // Refresh the palette chip + any placed grid cells whenever a field changes,
  // so the custom key's look stays in step with its definition.
  row.addEventListener('input', () => _sdRefreshCustom());
  row.addEventListener('change', () => _sdRefreshCustom());
  sync();
  return row;
}

function _sdAddOverrideRow(o) {
  const wrap = document.getElementById('streamdeck-overrides');
  if (wrap) wrap.appendChild(_sdOverrideRow(o));
  _sdRefreshCustom();
}

// Re-render the grid and palette (and refresh each custom row's placed badge)
// after a custom key is added, edited, deleted, or moved.
function _sdRefreshCustom() {
  const wrap = document.getElementById('streamdeck-overrides');
  if (wrap) {
    for (const row of wrap.querySelectorAll('.sd-override-row')) {
      const cid = row.dataset.cid;
      const placedAt = _sdKeys.map((n, i) => (n === cid ? i + 1 : 0)).filter(Boolean);
      const badge = row.querySelector('.sd-ov-placed');
      if (badge) {
        if (placedAt.length) {
          badge.className = 'sd-ov-placed badge bg-info';
          badge.textContent = 'Key ' + placedAt.join(', ');
        } else {
          badge.className = 'sd-ov-placed badge bg-secondary';
          badge.textContent = 'Not placed';
        }
      }
    }
  }
  _sdRenderGrid();
}

function _sdRenderOverrides(list) {
  // Load the custom-key library from saved overrides. Each saved entry carries a
  // stable id and (when placed) a slot; dedupe by id for the library and drop
  // placed ids onto the grid.
  const wrap = document.getElementById('streamdeck-overrides');
  if (!wrap) return;
  wrap.innerHTML = '';
  const seen = new Set();
  for (const o of (Array.isArray(list) ? list : [])) {
    const id = _sdIsCustomId(o.id) ? o.id : null;
    if (id && !seen.has(id)) {
      seen.add(id);
      wrap.appendChild(_sdOverrideRow({...o, id}));
    } else if (!id) {
      // Legacy override without an id: give it one so it joins the library.
      wrap.appendChild(_sdOverrideRow({...o}));
    }
    // Place this entry on its slot, if any (legacy and new both carry slot).
    const slot = parseInt(o.slot, 10);
    const cid = id || wrap.lastChild?.dataset.cid;
    if (cid && Number.isFinite(slot) && slot >= 0 && slot < _sdKeys.length) {
      _sdKeys[slot] = cid;
    }
  }
  _sdRefreshCustom();
}

// One custom key's definition from its editor row: {id, type, ...fields}, with
// no slot (placement lives on the grid). Returns null for an incomplete row.
function _sdDefFromRow(row) {
  const type = row.querySelector('.sd-ov-type')?.value || '';
  const id = row.dataset.cid;
  const label = row.querySelector('.sd-ov-label')?.value.trim() || '';
  const entry = {id, type};
  if (label) entry.label = label;
  if (type === 'ha_action') {
    const entity_id = row.querySelector('.sd-ov-entity')?.value.trim() || '';
    const service = row.querySelector('.sd-ov-service')?.value.trim() || '';
    if (!entity_id && !service) return null;
    if (entity_id) entry.entity_id = entity_id;
    if (service) entry.service = service;
    const color_on = row.querySelector('.sd-ov-color-on')?.value || '';
    const color_off = row.querySelector('.sd-ov-color-off')?.value || '';
    const icon = row.querySelector('.sd-ov-icon')?.value.trim() || '';
    if (color_on) entry.color_on = color_on;
    if (color_off) entry.color_off = color_off;
    if (icon) entry.icon = icon;
  } else if (type === 'timer') {
    entry.minutes = parseInt(row.querySelector('.sd-ov-minutes')?.value || '0', 10) || 0;
  } else if (type === 'weather') {
    const location = row.querySelector('.sd-ov-location')?.value.trim() || '';
    if (location) entry.location = location;
    const units = row.querySelector('.sd-ov-weather-units')?.value || '';
    if (units) entry.units = units;
    const wicon = row.querySelector('.sd-ov-weather-icon')?.value.trim() || '';
    if (wicon) entry.icon = wicon;
    if (row.querySelector('.sd-ov-forecast')?.checked) entry.forecast = true;
  } else if (type === 'shopping_add') {
    const item = row.querySelector('.sd-ov-item')?.value.trim() || '';
    if (!item) return null;
    entry.item = item;
  } else if (type === 'macro') {
    const actions = (row.querySelector('.sd-ov-actions')?.value || '')
      .split(',').map(s => s.trim()).filter(Boolean);
    if (actions.length < 1) return null;
    entry.actions = actions;
  } else if (type === 'camera') {
    const camera = row.querySelector('.sd-ov-camera')?.value.trim() || '';
    if (camera) entry.camera = camera;
    if (row.querySelector('.sd-ov-camera-full')?.checked) entry.full = true;
  } else if (type === 'media') {
    const entity_id = row.querySelector('.sd-ov-media-entity')?.value.trim() || '';
    if (!entity_id) return null;
    entry.entity_id = entity_id;
    entry.action = row.querySelector('.sd-ov-media-action')?.value || 'play_pause';
  } else {
    return null;
  }
  return entry;
}

// All defined custom keys (library), regardless of grid placement.
function _sdCustomDefs() {
  const wrap = document.getElementById('streamdeck-overrides');
  if (!wrap) return [];
  const out = [];
  for (const row of wrap.querySelectorAll('.sd-override-row')) {
    const def = _sdDefFromRow(row);
    if (def) out.push(def);
  }
  return out;
}

function _sdCustomDefById(id) {
  return _sdCustomDefs().find(d => d.id === id) || null;
}

// Save-time override list: each grid cell holding a custom key emits an override
// at that slot, and any custom key not placed anywhere is kept with slot -1 so
// the library survives a reload. The deck applies slot >= 0 and skips the rest.
function _sdCollectOverrides() {
  const defs = _sdCustomDefs();
  const byId = {};
  for (const d of defs) byId[d.id] = d;
  const out = [];
  const placed = new Set();
  _sdKeys.forEach((name, idx) => {
    if (byId[name]) {
      out.push({...byId[name], slot: idx});
      placed.add(name);
    }
  });
  for (const d of defs) {
    if (!placed.has(d.id)) out.push({...d, slot: -1});
  }
  return out;
}

// Collect the five Home Assistant key slots (ha_1..ha_5) into an ordered list
// of non-empty dicts. A row with no entity_id is dropped, but a leading empty
// row still shifts the keys (the deck maps the list to ha_1.. in order), so an
// empty entity row yields a placeholder dict to preserve later slots' positions
// only when a populated row follows it. Simpler: keep order, skip fully-empty
// rows; the deck assigns the remaining rows to ha_1, ha_2, ... in sequence.
function _sdCollectHaSlots() {
  const wrap = document.getElementById('streamdeck-ha-slots');
  if (!wrap) return [];
  const out = [];
  for (const row of wrap.querySelectorAll('.sd-ha-slot')) {
    const entity_id = row.querySelector('.sd-ha-entity')?.value.trim() || '';
    const service = row.querySelector('.sd-ha-service')?.value.trim() || '';
    const label = row.querySelector('.sd-ha-label')?.value.trim() || '';
    if (!entity_id && !service && !label) continue;
    const entry = {};
    if (entity_id) entry.entity_id = entity_id;
    if (service) entry.service = service;
    if (label) entry.label = label;
    out.push(entry);
  }
  return out;
}

// Cameras (FoodAssistant-oewn). Each row holds a Name, Stream URL, Snapshot URL,
// plus an optional Entity ID used only by the "From Home Assistant" helper to
// derive the two URLs from the HA base URL/token entered on this pane.
function _sdCameraRow(cam) {
  cam = cam || {};
  const div = document.createElement('div');
  div.className = 'row g-2 align-items-end sd-camera-row';
  div.innerHTML = `
    <div class="col-md-2">
      <label class="form-label small mb-1">Name</label>
      <input type="text" class="form-control form-control-sm sd-cam-name" placeholder="Front door">
    </div>
    <div class="col-md-3">
      <label class="form-label small mb-1">Stream URL</label>
      <input type="text" class="form-control form-control-sm sd-cam-stream" placeholder="http://.../stream.m3u8">
    </div>
    <div class="col-md-3">
      <label class="form-label small mb-1">Snapshot URL</label>
      <input type="text" class="form-control form-control-sm sd-cam-snapshot" placeholder="http://.../snapshot.jpg">
    </div>
    <div class="col-md-3">
      <label class="form-label small mb-1">Entity ID</label>
      <input type="text" class="form-control form-control-sm sd-cam-entity" placeholder="camera.front_door">
    </div>
    <div class="col-md-1 d-flex gap-1">
      <button type="button" class="btn btn-outline-secondary btn-sm" title="From Home Assistant" onclick="_sdCameraFromHa(this)"><i class="bi bi-house"></i></button>
      <button type="button" class="btn btn-outline-danger btn-sm" title="Remove" onclick="this.closest('.sd-camera-row').remove()"><i class="bi bi-trash"></i></button>
    </div>`;
  div.querySelector('.sd-cam-name').value = cam.name || '';
  div.querySelector('.sd-cam-stream').value = cam.stream_url || '';
  div.querySelector('.sd-cam-snapshot').value = cam.snapshot_url || '';
  div.querySelector('.sd-cam-entity').value = cam.ha_entity || '';
  return div;
}

function _sdAddCameraRow(cam) {
  const wrap = document.getElementById('streamdeck-cameras');
  if (wrap) wrap.appendChild(_sdCameraRow(cam));
}

// Read the HA base/token currently in the Interface > Home Assistant fields.
// A blank token means "use the one saved on the server" (the field is never
// pre-filled with the stored secret), so it is only sent when freshly typed.
function _haFormCreds() {
  const base = (document.getElementById('streamdeck_ha_base_url')?.value.trim() || '');
  const token = (document.getElementById('streamdeck_ha_token')?.value.trim() || '');
  return {base_url: base, token: token};
}

// Ask the server to list HA cameras (with URLs built from the saved or freshly
// typed token) so the browser never needs the secret. Returns {ok, cameras|error}.
async function _haDiscover() {
  const r = await fetch('setup/ha/cameras', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(_haFormCreds()),
  });
  return r.json();
}

// Fill a row's Stream and Snapshot URLs from its HA entity. The URLs are built
// on the server (which holds the token), so this works even when the token was
// saved earlier and is not in the form.
async function _sdCameraFromHa(btn) {
  const row = btn.closest('.sd-camera-row');
  if (!row) return;
  const entity = row.querySelector('.sd-cam-entity')?.value.trim() || '';
  if (!entity) return;
  const orig = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  try {
    const data = await _haDiscover();
    if (data && data.ok) {
      const match = (data.cameras || []).find(c => c.entity_id === entity);
      if (match) {
        row.querySelector('.sd-cam-stream').value = match.stream_url || '';
        row.querySelector('.sd-cam-snapshot').value = match.snapshot_url || '';
        if (!row.querySelector('.sd-cam-name').value.trim()) {
          row.querySelector('.sd-cam-name').value = match.name || '';
        }
      }
    }
  } catch (e) { /* leave the URLs for the user to paste */ }
  finally { btn.disabled = false; btn.innerHTML = orig; }
}

// Preview a discovered camera in a modal before adding it (FoodAssistant-kval).
// HA cameras are fetched through the server proxy (bearer token); an IP camera's
// snapshot URL is loaded directly.
function _camPreview(cam) {
  let modal = document.getElementById('cam-preview-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'cam-preview-modal';
    modal.className = 'modal fade';
    modal.tabIndex = -1;
    modal.innerHTML =
      '<div class="modal-dialog modal-dialog-centered modal-lg"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title" id="cam-preview-title">Camera</h5>' +
          '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '<div class="modal-body text-center">' +
          '<img id="cam-preview-img" class="img-fluid rounded" alt="Camera preview" style="max-height:70vh">' +
          '<div id="cam-preview-msg" class="text-secondary small mt-2 d-none"></div>' +
        '</div>' +
      '</div></div>';
    document.body.appendChild(modal);
  }
  document.getElementById('cam-preview-title').textContent = cam.name || cam.entity_id || 'Camera';
  const img = document.getElementById('cam-preview-img');
  const msg = document.getElementById('cam-preview-msg');
  msg.classList.add('d-none');
  img.classList.remove('d-none');
  const params = new URLSearchParams();
  if (cam.entity_id) params.set('entity', cam.entity_id);
  if (cam.snapshot_url) params.set('snapshot_url', cam.snapshot_url);
  img.src = 'ui/camera/preview?' + params.toString() + '&_=' + Date.now();
  img.onerror = () => {
    img.classList.add('d-none');
    msg.textContent = 'Could not load a snapshot from this camera.';
    msg.classList.remove('d-none');
  };
  new bootstrap.Modal(modal).show();
}

// Discover all HA cameras and list them with an Add button each. Already-added
// entities are skipped so the list only offers cameras not yet configured.
async function _sdDiscoverCameras(btn) {
  const out = document.getElementById('cameras-discovered');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Searching...'; }
  if (out) out.innerHTML = '';
  try {
    const data = await _haDiscover();
    if (!data || !data.ok) {
      if (out) out.innerHTML = `<span class="text-danger small">${(data && data.error) || 'Discovery failed.'}</span>`;
      return;
    }
    const cams = data.cameras || [];
    if (!cams.length) {
      if (out) out.innerHTML = '<span class="text-secondary small">No camera entities found on Home Assistant.</span>';
      return;
    }
    // Dedup by entity id: an HA camera is bound by its entity, and the server
    // proxies it with the bearer token, so the URLs are not stored.
    const have = new Set(_sdCollectCameras().map(c => c.ha_entity).filter(Boolean));
    const list = document.createElement('div');
    list.className = 'list-group';
    cams.forEach(cam => {
      const already = have.has(cam.entity_id);
      const item = document.createElement('div');
      item.className = 'list-group-item d-flex justify-content-between align-items-center py-1';
      const label = document.createElement('span');
      label.className = 'small';
      label.textContent = `${cam.name} (${cam.entity_id})`;
      const btns = document.createElement('div');
      btns.className = 'd-flex gap-1';
      // Preview the discovered camera before adding it (FoodAssistant-kval).
      const prev = document.createElement('button');
      prev.type = 'button';
      prev.className = 'btn btn-sm btn-outline-info';
      prev.innerHTML = '<i class="bi bi-eye"></i> Preview';
      prev.onclick = () => _camPreview(cam);
      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'btn btn-sm ' + (already ? 'btn-outline-secondary' : 'btn-outline-success');
      add.innerHTML = already ? '<i class="bi bi-check2"></i> Added' : '<i class="bi bi-plus-lg"></i> Add';
      add.disabled = already;
      add.onclick = () => {
        _sdAddCameraRow({name: cam.name, ha_entity: cam.entity_id});
        add.disabled = true;
        add.className = 'btn btn-sm btn-outline-secondary';
        add.innerHTML = '<i class="bi bi-check2"></i> Added';
      };
      btns.appendChild(prev);
      btns.appendChild(add);
      item.appendChild(label);
      item.appendChild(btns);
      list.appendChild(item);
    });
    if (out) { out.appendChild(list); }
  } catch (e) {
    if (out) out.innerHTML = `<span class="text-danger small">${e}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

// Collect the camera rows into a list of non-empty dicts. A row needs at least a
// name or one URL to count; blank rows are skipped.
function _sdCollectCameras() {
  const wrap = document.getElementById('streamdeck-cameras');
  if (!wrap) return [];
  const out = [];
  for (const row of wrap.querySelectorAll('.sd-camera-row')) {
    const name = row.querySelector('.sd-cam-name')?.value.trim() || '';
    // A Reolink camera keeps its login on the server, so its row carries only the
    // host/channel/user (no password). Emit those; the server restores the stored
    // password on save so re-saving the list does not wipe the credentials.
    if (row.querySelector('.sd-cam-source')?.value === 'reolink') {
      out.push({
        name,
        source: 'reolink',
        host: row.querySelector('.sd-cam-host')?.value.trim() || '',
        port: row.querySelector('.sd-cam-port')?.value.trim() || '',
        channel: parseInt(row.querySelector('.sd-cam-channel')?.value || '0', 10) || 0,
        username: row.querySelector('.sd-cam-username')?.value.trim() || '',
        stream_quality: row.querySelector('.sd-cam-quality')?.value.trim() || 'main',
      });
      continue;
    }
    const stream_url = row.querySelector('.sd-cam-stream')?.value.trim() || '';
    const snapshot_url = row.querySelector('.sd-cam-snapshot')?.value.trim() || '';
    const ha_entity = row.querySelector('.sd-cam-entity')?.value.trim() || '';
    if (!name && !stream_url && !snapshot_url && !ha_entity) continue;
    const entry = {name, stream_url, snapshot_url};
    // An HA entity binds the camera to Home Assistant; the server proxies it with
    // the bearer token, so no stream/snapshot URL needs to be stored or correct.
    if (ha_entity) entry.ha_entity = ha_entity;
    out.push(entry);
  }
  return out;
}

// Migrate HA settings from a legacy deck config into the form. Credentials and
// the slot rows are now server-rendered from app settings (the source of truth),
// so this only fills a field that the server left blank: a deck configured before
// the credentials moved to the server still surfaces its values here for one save.
function _sdLoadHaSettings(cfg) {
  cfg = cfg || {};
  const urlEl = document.getElementById('streamdeck_ha_base_url');
  if (urlEl && !urlEl.value && cfg.ha_base_url) urlEl.value = cfg.ha_base_url;
  const wrap = document.getElementById('streamdeck-ha-slots');
  if (!wrap) return;
  const slots = Array.isArray(cfg.ha_slots) ? cfg.ha_slots : [];
  const rows = wrap.querySelectorAll('.sd-ha-slot');
  rows.forEach((row, i) => {
    const slot = slots[i] || {};
    const e = row.querySelector('.sd-ha-entity');
    const s = row.querySelector('.sd-ha-service');
    const l = row.querySelector('.sd-ha-label');
    if (e && !e.value) e.value = slot.entity_id || '';
    if (s && !s.value) s.value = slot.service || '';
    if (l && !l.value) l.value = slot.label || '';
  });
}

// Save just the Stream Deck weather. Lives outside the "Stream Deck connected"
// gate so a main server with no local deck can set the location its satellite
// decks pull. Persists to settings, and when a deck is attached to THIS device
// also pushes the change straight into its config.toml.
async function saveStreamDeckWeather(btn) {
  const setRes = (html) => document.querySelectorAll('.sd-weather-result')
    .forEach(e => { e.innerHTML = html; });
  setRes('<span class="text-secondary">Saving...</span>');
  const weather_location = document.getElementById('streamdeck_weather_location')?.value || '';
  const weather_units = document.getElementById('streamdeck_weather_units')?.value || 'f';
  const weather_api_base = document.getElementById('weather_api_base')?.value.trim() || '';
  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({streamdeck_weather_location: weather_location,
                            streamdeck_weather_units: weather_units,
                            weather_api_base: weather_api_base}),
    });
    if (document.getElementById('has_streamdeck')?.checked) {
      try {
        const cur = await fetch('setup/streamdeck/config').then(x => x.json());
        const base = (cur && cur.ok && cur.config && typeof cur.config === 'object') ? cur.config : {};
        const merged = Object.assign({}, base, {weather_location, weather_units});
        await fetch('setup/streamdeck/config', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({config: merged}),
        });
      } catch (e) { /* no local bridge: settings save is enough for satellites */ }
    }
    setRes('<span class="text-success">Saved.</span>');
  } catch (e) {
    setRes('<span class="text-danger">Save failed: ' + e.message + '</span>');
  }
}

async function saveStreamDeckSettings() {
  // Mirror the status to every save-result slot (top and bottom of the editor)
  // so it is visible wherever the user clicked Save.
  const setRes = (html) => document.querySelectorAll('.sd-save-result')
    .forEach(e => { e.innerHTML = html; });
  setRes('<span class="text-secondary">Saving...</span>');
  const has_streamdeck = document.getElementById('has_streamdeck')?.checked || false;
  const streamdeck_key_count = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
  const streamdeck_idle_timeout = parseInt(document.getElementById('streamdeck_idle_timeout')?.value || '0', 10);
  const logoOffEl = document.getElementById('streamdeck_logo_when_display_off');
  const streamdeck_logo_when_display_off = logoOffEl ? logoOffEl.checked : true;
  const rotation = parseInt(document.getElementById('streamdeck_rotation')?.value || '0', 10);
  const brightness = parseInt(document.getElementById('streamdeck_brightness')?.value || '60', 10);
  const keys = _sdCollectKeys();
  const weather_location = document.getElementById('streamdeck_weather_location')?.value || '';
  const weather_units = document.getElementById('streamdeck_weather_units')?.value || 'f';
  const key_style = document.getElementById('streamdeck_key_style')?.value || 'rich';
  const icon_color = document.getElementById('streamdeck_icon_color')?.value || 'full';
  const key_overrides = _sdCollectOverrides();
  // HA key bindings save with the deck settings; the URL/token live in the
  // Interface > Home Assistant pane and are stamped into the deck config by the
  // server, so they are not posted here. Cameras are owned by the Cameras pane.
  const ha_slots = _sdCollectHaSlots();

  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({has_streamdeck, streamdeck_key_count, streamdeck_idle_timeout,
                            streamdeck_logo_when_display_off,
                            streamdeck_rotation: rotation,
                            streamdeck_key_overrides: key_overrides,
                            streamdeck_weather_location: weather_location,
                            streamdeck_weather_units: weather_units,
                            streamdeck_key_style: key_style,
                            streamdeck_icon_color: icon_color,
                            streamdeck_ha_slots: ha_slots}),
    });

    if (has_streamdeck) {
      // Read-modify-write: start from the current on-disk config so keys we do
      // not edit here (the host bridge rewrites the whole config.toml from this
      // posted dict, it does not merge) survive the save. If the GET fails, fall
      // back to posting just the edited fields, as the old behaviour did. The
      // server stamps ha_base_url/ha_token/ha_slots/cameras from settings.
      let base = {};
      try {
        const cur = await fetch('setup/streamdeck/config').then(x => x.json());
        if (cur && cur.ok && cur.config && typeof cur.config === 'object') {
          base = cur.config;
        }
      } catch (e) {
        base = {};
      }
      const merged = Object.assign({}, base, {
        rotation,
        brightness,
        keys: keys.length > 0 ? keys : undefined,
        weather_location,
        weather_units,
        key_overrides,
      });
      await fetch('setup/streamdeck/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({config: merged}),
      });
      // Automatically restart to apply config changes
      await fetch('setup/streamdeck/restart', {method: 'POST'});
    }
    
    setRes('<span class="text-success"><i class="bi bi-check-circle me-1"></i>Stream Deck settings saved.</span>');
  } catch (e) {
    setRes(`<span class="text-danger">${e}</span>`);
  }
}


async function restartStreamDeck() {
  const el = document.getElementById('streamdeck-save-result');
  if (el) el.innerHTML = '<span class="text-secondary">Restarting service...</span>';
  try {
    const r = await fetch('setup/streamdeck/restart', {method: 'POST'});
    const d = await r.json();
    if (el) el.innerHTML = d.ok
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Stream Deck service restarted.</span>'
      : `<span class="text-danger">${d.error}</span>`;
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

// Stream Deck profile management (FoodAssistant-aqa)
let _sdProfiles = [];

async function _sdLoadProfiles() {
  try {
    const d = await fetch('setup/streamdeck/profiles').then(r => r.json());
    _sdProfiles = (d.profiles || []);
    _sdPopulateProfileSelect();
  } catch (e) {
    // profile list unavailable: leave select empty
  }
}

function _sdPopulateProfileSelect() {
  const sel = document.getElementById('sd-profile-select');
  if (!sel) return;
  const cur = sel.value;
  const keyCount = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
  sel.innerHTML = '<option value="">(no profile selected)</option>';
  for (const p of _sdProfiles) {
    if (p.deck_size !== keyCount) continue;
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.name;
    if (p.name === cur) opt.selected = true;
    sel.appendChild(opt);
  }
  _sdProfileSelectionChanged();
}

function _sdProfileSelectionChanged() {
  const sel = document.getElementById('sd-profile-select');
  const hasVal = sel && sel.value !== '';
  const loadBtn = document.getElementById('btn-sd-profile-load');
  const delBtn = document.getElementById('btn-sd-profile-delete');
  if (loadBtn) loadBtn.disabled = !hasVal;
  if (delBtn) delBtn.disabled = !hasVal;
}

async function _sdSaveProfile() {
  const el = document.getElementById('sd-profile-result');
  const name = (document.getElementById('sd-profile-name-input')?.value || '').trim();
  if (!name) {
    if (el) el.innerHTML = '<span class="text-warning">Enter a profile name first.</span>';
    return;
  }
  const keyCount = parseInt(document.getElementById('streamdeck_key_count')?.value || '15', 10);
  const key_overrides = _sdCollectOverrides();
  if (el) el.innerHTML = '<span class="text-secondary">Saving...</span>';
  try {
    const r = await fetch('setup/streamdeck/profiles', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, deck_size: keyCount, key_overrides}),
    });
    const d = await r.json();
    if (d.ok) {
      if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Profile "${name}" saved.</span>`;
      await _sdLoadProfiles();
      const sel = document.getElementById('sd-profile-select');
      if (sel) sel.value = name;
      _sdProfileSelectionChanged();
    } else {
      if (el) el.innerHTML = `<span class="text-danger">${d.error || 'Save failed.'}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}

async function _sdLoadProfile() {
  const el = document.getElementById('sd-profile-result');
  const sel = document.getElementById('sd-profile-select');
  const name = sel?.value || '';
  if (!name) return;
  const profile = _sdProfiles.find(p => p.name === name);
  if (!profile) {
    if (el) el.innerHTML = '<span class="text-warning">Profile not found.</span>';
    return;
  }
  if (el) el.innerHTML = '<span class="text-secondary">Loading...</span>';
  // Apply profile key_overrides into the override rows
  const container = document.getElementById('streamdeck-overrides');
  if (container) {
    container.innerHTML = '';
    for (const o of (profile.key_overrides || [])) _sdAddOverrideRow(o);
  }
  if (el) el.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Profile "${name}" applied. Save Stream Deck settings to commit.</span>`;
}

async function _sdDeleteProfile() {
  const el = document.getElementById('sd-profile-result');
  const sel = document.getElementById('sd-profile-select');
  const name = sel?.value || '';
  if (!name) return;
  if (!confirm(`Delete profile "${name}"?`)) return;
  if (el) el.innerHTML = '<span class="text-secondary">Deleting...</span>';
  try {
    const r = await fetch(`setup/streamdeck/profiles/${encodeURIComponent(name)}`, {method: 'DELETE'});
    const d = await r.json();
    if (d.ok) {
      if (el) el.innerHTML = `<span class="text-success">Profile "${name}" deleted.</span>`;
      await _sdLoadProfiles();
    } else {
      if (el) el.innerHTML = `<span class="text-danger">${d.error || 'Delete failed.'}</span>`;
    }
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger">${e}</span>`;
  }
}
