// Label designer (FoodAssistant-or5e + the UI half of FoodAssistant-bwl1).
//
// A drag-and-drop field-placement designer for the Printing pane. The left-hand
// canvas is an editable schematic: each field is a draggable, resizable box laid
// out in fractions (0..1) of the label's printable (inside-the-margins) area,
// exactly as the render engine reads them. The right-hand image is the real
// rendered label from POST /printing/label/layout/preview, so what you place is
// what prints. A format dropdown (from /printing/label/presets) picks a common
// stock size and seeds a matching starting design.
//
// Everything here degrades quietly: a failed fetch shows a small message, never
// a broken image, and a designer that cannot initialise never blocks the page.
// It relies only on globals already loaded (val, chk, refreshLabelPreview,
// refreshDecorativePreview from helpers.js / panes.js).

// Friendly names for each supported field. The keys are the render engine's
// field ids; only these may be placed.
const LD_FIELD_LABELS = {
  name: 'Name',
  added: 'Added',
  best_by: 'Best by',
  best_by_date: 'Best by date',
  best_by_badge: 'AI/est. chip',
  extra: 'Extra note',
  quantity: 'Quantity',
  location: 'Location',
  static: 'Custom text',
  barcode: 'Barcode',
  qr: 'QR code',
  icon: 'Icon / symbol',
};

// The order fields appear in the add-a-field palette.
const LD_PALETTE_ORDER = ['name', 'added', 'best_by', 'best_by_date',
  'best_by_badge', 'extra', 'quantity', 'location', 'static', 'barcode', 'qr', 'icon'];

// The curated icon/symbol keys (mirrors services/label_render.py ICON_GLYPHS).
// Fetched fresh from /printing/decorative/icons on init so the glyphs always
// match the backend, but seeded here so the inspector has something to show
// before that request lands.
let LD_ICONS = [
  { key: 'snowflake', glyph: '❄' },
  { key: 'warning', glyph: '⚠' },
  { key: 'star', glyph: '★' },
  { key: 'check', glyph: '✓' },
  { key: 'hourglass', glyph: '⌛' },
  { key: 'coffee', glyph: '☕' },
  { key: 'sun', glyph: '☀' },
  { key: 'scissors', glyph: '✂' },
];

// QR payload kinds (mirrors label_render.QR_PAYLOAD_KINDS).
const LD_QR_KINDS = [
  { key: 'url', label: 'URL / text' },
  { key: 'text', label: 'Plain text' },
  { key: 'vcard', label: 'Contact card (vCard)' },
];

// The shipped default food design, expressed as layout elements. Mirrors
// services/label_render.py default_food_layout: the element fractions are
// size-independent, so the same list seeds any stock size. Used by "Reset to
// default" and as a fallback when nothing is saved.
const LD_DEFAULT_ELS = [
  { field: 'name', x: 0, y: 0, w: 1, h: 0.40, align: 'left', bold: true, size_scale: 1, text: '', uppercase: false, outline: false, qr_kind: 'url', qr_extra: '' },
  { field: 'static', text: 'BEST BY', x: 0, y: 0.46, w: 0.6, h: 0.12, align: 'left', bold: true, size_scale: 1, uppercase: true, outline: false, qr_kind: 'url', qr_extra: '' },
  { field: 'best_by_badge', x: 0.6, y: 0.44, w: 0.4, h: 0.14, align: 'right', bold: false, size_scale: 1, text: '', uppercase: false, outline: false, qr_kind: 'url', qr_extra: '' },
  { field: 'best_by_date', x: 0, y: 0.58, w: 1, h: 0.24, align: 'left', bold: true, size_scale: 1, text: '', uppercase: false, outline: false, qr_kind: 'url', qr_extra: '' },
  { field: 'added', x: 0, y: 0.86, w: 1, h: 0.12, align: 'left', bold: false, size_scale: 1, text: '', uppercase: false, outline: false, qr_kind: 'url', qr_extra: '' },
];

// The white border kept clear on every side, in inches. Matches the render
// engine default so the schematic's margin guide lines up with the print.
const LD_MARGIN_IN = 0.06;

let _ldEls = [];          // current elements
let _ldSel = -1;          // selected element index (-1 = none)
let _ldInited = false;    // one-time setup guard
let _ldPresets = [];      // fetched format presets (with layouts)
let _ldNamedPresets = []; // fetched saved layout presets (FoodAssistant-rhqa)
let _ldPreviewTimer = null;
let _ldDrag = null;       // active drag/resize state
let _ldDirty = false;     // true once the design has changed since the last save

// Unsaved-changes guard (FoodAssistant-shq5). A layout edit only lives in
// _ldEls until "Save layout" posts it; leaving the pane, closing the tab, or
// switching to another settings pane before that would silently drop the
// change, so both paths are guarded here. Scoped to the label designer only:
// a plain settings pane can be left freely.
function ldMarkDirty() { _ldDirty = true; }
function ldClearDirty() { _ldDirty = false; }

window.addEventListener('beforeunload', function (e) {
  if (!_ldDirty) return;
  e.preventDefault();
  e.returnValue = '';
  return '';
});

// Confirm before switching off the Printing pane with an unsaved design.
// Bootstrap fires "hide.bs.tab" on the tab being left, before it fires the
// "shown.bs.tab" on the new one; hide.bs.tab is cancelable, so declining the
// confirm keeps the Printing pane open.
(function ldInstallTabGuard() {
  const pill = document.querySelector('.side-menu [data-bs-target="#pane-printing"]');
  if (!pill) return;
  pill.addEventListener('hide.bs.tab', function (ev) {
    if (!_ldDirty) return;
    const leave = window.confirm(
      'Your label design has unsaved changes. Leave this page and lose them?');
    if (!leave) ev.preventDefault();
  });
})();

// Open handler: safe to call every time the pane is shown. First open builds the
// palette, loads the saved design, fetches presets, and draws. Later opens just
// re-sync the enabled gate. Never throws (so a designer failure cannot block the
// Printing pane).
function ldInit() {
  try {
    ldSyncGate();
    if (_ldInited) { ldRender(); return; }
    _ldInited = true;
    ldBuildPalette();
    ldLoadSaved();
    ldApplyShapeState();
    ldRender();
    ldSchedulePreview();
    ldFetchPresets();
    ldFetchIcons();
    ldFetchNamedPresets();
  } catch (_) { /* the designer must never block the page */ }
}

// Show the designer only when printing is turned on, matching the rest of the
// pane. Reads the live master-switch checkbox so flipping it reveals or hides
// the designer without a reload.
function ldSyncGate() {
  const on = !!chk('printing_enabled');
  const body = document.getElementById('label-designer');
  const off = document.getElementById('label-designer-gate-off');
  if (body) body.style.display = on ? '' : 'none';
  if (off) off.style.display = on ? 'none' : '';
}

// -- Element helpers --------------------------------------------------------

function ldClamp01(value, dflt) {
  const f = parseFloat(value);
  if (isNaN(f)) return dflt;
  return Math.min(1, Math.max(0, f));
}

function ldRound(v) { return Math.round(v * 1000) / 1000; }

// Coerce an untrusted element dict into the shape the designer holds, or null if
// its field is not one we can place. Clamps fractions to [0, 1].
function ldNormalizeEl(raw) {
  if (!raw || LD_FIELD_LABELS[raw.field] === undefined) return null;
  let scale = parseFloat(raw.size_scale);
  if (isNaN(scale) || scale <= 0) scale = 1;
  const align = ['left', 'center', 'right'].includes(raw.align) ? raw.align : 'left';
  const qrKinds = LD_QR_KINDS.map(k => k.key);
  const qrKind = qrKinds.includes(raw.qr_kind) ? raw.qr_kind : 'url';
  return {
    field: raw.field,
    x: ldClamp01(raw.x, 0),
    y: ldClamp01(raw.y, 0),
    w: ldClamp01(raw.w, 1) || 1,
    h: ldClamp01(raw.h, 1) || 1,
    align: align,
    bold: !!raw.bold,
    size_scale: scale,
    text: (raw.text != null ? String(raw.text) : ''),
    uppercase: !!raw.uppercase,
    outline: !!raw.outline,
    qr_kind: qrKind,
    qr_extra: (raw.qr_extra != null ? String(raw.qr_extra) : ''),
  };
}

// Load the saved custom layout embedded in the page, falling back to the default
// food design when nothing is saved (or the saved blob is unusable).
function ldLoadSaved() {
  const node = document.getElementById('ld-saved-layout');
  const raw = node ? (node.textContent || '').trim() : '';
  _ldEls = [];
  if (raw) {
    try {
      const data = JSON.parse(raw);
      if (data && Array.isArray(data.elements)) {
        _ldEls = data.elements.map(ldNormalizeEl).filter(Boolean);
      }
    } catch (_) { /* fall through to the default */ }
  }
  if (!_ldEls.length) _ldEls = LD_DEFAULT_ELS.map(e => Object.assign({}, e));
}

// The chosen label shape: "rectangle" (default), "square", or "round".
function ldShape() {
  const sel = document.getElementById('label_shape');
  return (sel && sel.value) || 'rectangle';
}

// Square and round labels are die-cut 1:1, so their two sides are equal.
function ldShapeIsSquareSided() {
  const shape = ldShape();
  return shape === 'square' || shape === 'round';
}

// The current stock size, read from the shared size fields. For a square or
// round label the height follows the width, so the canvas, preview, and saved
// layout all stay 1:1 no matter what the (disabled) height field shows.
function ldGetSize() {
  const w = parseFloat(val('label_width_in')) || 2.0;
  let h = parseFloat(val('label_height_in')) || 1.0;
  if (ldShapeIsSquareSided()) h = w;
  return {
    w: w,
    h: h,
    dpi: parseInt(val('label_dpi'), 10) || 203,
  };
}

// React to a label-shape change: for square/round keep height equal to width
// and lock the height field; for round show the circular safe-area guide. Then
// re-render the canvas and preview at the new aspect.
// Apply the height-field lock for the current shape (no render). Square/round
// keep height equal to width and disable the height field; rectangle frees it.
function ldApplyShapeState() {
  const hIn = document.getElementById('label_height_in');
  if (!hIn) return;
  if (ldShapeIsSquareSided()) {
    hIn.value = parseFloat(val('label_width_in')) || 2.0;
    hIn.disabled = true;
  } else {
    hIn.disabled = false;
  }
}

function ldOnShapeChange() {
  ldApplyShapeState();
  ldMarkDirty();
  ldRender();
  if (typeof refreshLabelPreview === 'function') refreshLabelPreview();
  ldSchedulePreview();
}

// The layout dict the backend expects, built from the current size and elements.
function ldLayoutDict() {
  const size = ldGetSize();
  return {
    width_in: size.w,
    height_in: size.h,
    dpi: size.dpi,
    margin_in: LD_MARGIN_IN,
    elements: _ldEls.map(el => ({
      field: el.field,
      x: ldRound(el.x), y: ldRound(el.y), w: ldRound(el.w), h: ldRound(el.h),
      align: el.align,
      bold: !!el.bold,
      size_scale: el.size_scale,
      text: el.text || '',
      uppercase: !!el.uppercase,
      outline: !!el.outline,
      qr_kind: el.qr_kind || 'url',
      qr_extra: el.qr_extra || '',
    })),
  };
}

// -- Palette ----------------------------------------------------------------

function ldBuildPalette() {
  const p = document.getElementById('ld-palette');
  if (!p) return;
  p.innerHTML = LD_PALETTE_ORDER.map(f =>
    `<button type="button" class="btn btn-outline-secondary btn-sm" onclick="ldAddField('${f}')">` +
    `<i class="bi bi-plus-lg me-1"></i>${LD_FIELD_LABELS[f]}</button>`).join('');
}

// Drop a new field near the middle of the label and select it.
function ldAddField(field) {
  if (LD_FIELD_LABELS[field] === undefined) return;
  const el = {
    field: field, x: 0.3, y: 0.4, w: 0.4, h: 0.2,
    align: (field === 'name' || field === 'qr' || field === 'barcode' || field === 'icon') ? 'center' : 'left',
    bold: (field === 'name'), size_scale: 1, text: '', uppercase: false,
    outline: false, qr_kind: 'url', qr_extra: '',
  };
  if (field === 'static') el.text = 'Text';
  if (field === 'icon') el.text = (LD_ICONS[0] && LD_ICONS[0].key) || 'star';
  _ldEls.push(el);
  _ldSel = _ldEls.length - 1;
  ldMarkDirty();
  ldRender();
  ldSchedulePreview();
}

// -- Canvas rendering -------------------------------------------------------

// A readable label for a box: a custom-text box shows its text.
function ldBoxLabel(el) {
  if (el.field === 'static') return el.text || 'Custom text';
  if (el.field === 'icon') {
    const found = LD_ICONS.find(i => i.key === el.text);
    return found ? (found.glyph + ' ' + found.key) : 'Icon';
  }
  return LD_FIELD_LABELS[el.field] || el.field;
}

function ldRender() {
  const canvas = document.getElementById('ld-canvas');
  const inner = document.getElementById('ld-inner');
  if (!canvas || !inner) return;
  const size = ldGetSize();
  // The canvas takes the label's aspect ratio; the inner box is inset by the
  // margin (as a fraction of each axis, so it lines up with the print).
  canvas.style.aspectRatio = size.w + ' / ' + size.h;
  // Round stock: show the circular safe-area guide (the canvas is 1:1 here, so
  // an inset:0 circle is the inscribed printable area).
  const guide = document.getElementById('ld-round-guide');
  if (guide) guide.style.display = (ldShape() === 'round') ? 'block' : 'none';
  const mx = Math.min(45, (LD_MARGIN_IN / size.w) * 100);
  const my = Math.min(45, (LD_MARGIN_IN / size.h) * 100);
  inner.style.left = mx + '%';
  inner.style.right = mx + '%';
  inner.style.top = my + '%';
  inner.style.bottom = my + '%';

  inner.innerHTML = '';
  _ldEls.forEach((el, i) => {
    const box = document.createElement('div');
    box.className = 'ld-box' + (i === _ldSel ? ' ld-selected' : '');
    box.style.left = (el.x * 100) + '%';
    box.style.top = (el.y * 100) + '%';
    box.style.width = (el.w * 100) + '%';
    box.style.height = (el.h * 100) + '%';
    box.style.textAlign = el.align;
    if (el.bold) box.style.fontWeight = '700';

    const lbl = document.createElement('span');
    lbl.className = 'ld-box-label';
    lbl.textContent = ldBoxLabel(el);
    box.appendChild(lbl);

    const handle = document.createElement('div');
    handle.className = 'ld-handle';
    box.appendChild(handle);

    box.addEventListener('pointerdown', (ev) => ldPointerDown(ev, i, false));
    handle.addEventListener('pointerdown', (ev) => ldPointerDown(ev, i, true));
    inner.appendChild(box);
  });
  ldRenderInspector();
}

// Update just the selection highlight and inspector without rebuilding the DOM
// (used mid-drag so the box holding the pointer capture survives).
function ldUpdateSelectionUI() {
  const inner = document.getElementById('ld-inner');
  if (inner) {
    Array.from(inner.children).forEach((c, i) =>
      c.classList.toggle('ld-selected', i === _ldSel));
  }
  ldRenderInspector();
}

// -- Drag and resize (pointer events cover mouse and touch) -----------------

function ldPointerDown(ev, idx, resize) {
  ev.preventDefault();
  ev.stopPropagation();
  const inner = document.getElementById('ld-inner');
  if (!inner) return;
  const rect = inner.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;
  const el = _ldEls[idx];
  if (!el) return;
  _ldSel = idx;
  ldUpdateSelectionUI();
  _ldDrag = {
    idx: idx, resize: resize,
    sx: ev.clientX, sy: ev.clientY,
    ox: el.x, oy: el.y, ow: el.w, oh: el.h,
    rw: rect.width, rh: rect.height,
    box: inner.children[idx],
  };
  try { ev.target.setPointerCapture(ev.pointerId); } catch (_) { /* older browsers */ }
  window.addEventListener('pointermove', ldPointerMove);
  window.addEventListener('pointerup', ldPointerUp);
}

function ldPointerMove(ev) {
  if (!_ldDrag) return;
  const d = _ldDrag;
  const el = _ldEls[d.idx];
  if (!el) return;
  const dx = (ev.clientX - d.sx) / d.rw;
  const dy = (ev.clientY - d.sy) / d.rh;
  const MIN = 0.05;
  if (d.resize) {
    // Grow/shrink from the top-left corner; never past the label edge.
    el.w = Math.min(1 - el.x, Math.max(MIN, d.ow + dx));
    el.h = Math.min(1 - el.y, Math.max(MIN, d.oh + dy));
  } else {
    // Move; clamp so the whole box stays inside the printable area.
    el.x = Math.min(1 - el.w, Math.max(0, d.ox + dx));
    el.y = Math.min(1 - el.h, Math.max(0, d.oy + dy));
  }
  if (d.box) {
    d.box.style.left = (el.x * 100) + '%';
    d.box.style.top = (el.y * 100) + '%';
    d.box.style.width = (el.w * 100) + '%';
    d.box.style.height = (el.h * 100) + '%';
  }
}

function ldPointerUp() {
  window.removeEventListener('pointermove', ldPointerMove);
  window.removeEventListener('pointerup', ldPointerUp);
  if (_ldDrag) {
    const el = _ldEls[_ldDrag.idx];
    if (el) {
      el.x = ldRound(el.x); el.y = ldRound(el.y);
      el.w = ldRound(el.w); el.h = ldRound(el.h);
    }
    ldMarkDirty();
  }
  _ldDrag = null;
  ldRender();
  ldSchedulePreview();
}

// -- Selected-field inspector ----------------------------------------------

function ldSelEl() {
  return (_ldSel >= 0 && _ldSel < _ldEls.length) ? _ldEls[_ldSel] : null;
}

function ldRenderInspector() {
  const insp = document.getElementById('ld-inspector');
  if (!insp) return;
  const el = ldSelEl();
  if (!el) { insp.style.display = 'none'; return; }
  insp.style.display = '';

  const title = document.getElementById('ld-insp-title');
  if (title) title.textContent = LD_FIELD_LABELS[el.field] || el.field;

  // The text row is only meaningful for the fields that carry their own value.
  const usesText = ['static', 'barcode', 'qr'].includes(el.field);
  const textRow = document.getElementById('ld-insp-text-row');
  if (textRow) textRow.style.display = usesText ? '' : 'none';
  const textIn = document.getElementById('ld-insp-text');
  if (textIn) {
    textIn.value = el.text || '';
    textIn.placeholder = el.field === 'qr'
      ? (el.qr_kind === 'vcard' ? 'Contact name' : 'QR text (blank uses the name)')
      : el.field === 'barcode' ? 'Barcode value (blank uses the name)'
        : 'Text to print';
  }

  // Icon picker: only meaningful for the "icon" field.
  const iconRow = document.getElementById('ld-insp-icon-row');
  if (iconRow) iconRow.style.display = el.field === 'icon' ? '' : 'none';
  const iconSel = document.getElementById('ld-insp-icon');
  if (iconSel && el.field === 'icon') iconSel.value = el.text || '';

  // QR payload kind + optional note: only meaningful for the "qr" field.
  const qrKindRow = document.getElementById('ld-insp-qr-kind-row');
  if (qrKindRow) qrKindRow.style.display = el.field === 'qr' ? '' : 'none';
  const qrKindSel = document.getElementById('ld-insp-qr-kind');
  if (qrKindSel && el.field === 'qr') qrKindSel.value = el.qr_kind || 'url';
  const qrNoteRow = document.getElementById('ld-insp-qr-note-row');
  if (qrNoteRow) qrNoteRow.style.display = (el.field === 'qr' && el.qr_kind === 'vcard') ? '' : 'none';
  const qrNoteIn = document.getElementById('ld-insp-qr-note');
  if (qrNoteIn) qrNoteIn.value = el.qr_extra || '';

  insp.querySelectorAll('[data-align]').forEach(b =>
    b.classList.toggle('active', b.dataset.align === el.align));
  const size = document.getElementById('ld-insp-size');
  if (size) size.value = el.size_scale;
  const bold = document.getElementById('ld-insp-bold');
  if (bold) bold.checked = !!el.bold;
  const upper = document.getElementById('ld-insp-upper');
  if (upper) upper.checked = !!el.uppercase;
  const outline = document.getElementById('ld-insp-outline');
  if (outline) outline.checked = !!el.outline;
}

function ldSetAlign(a) {
  const el = ldSelEl(); if (!el) return;
  el.align = a; ldMarkDirty(); ldRender(); ldSchedulePreview();
}
function ldInspBoldChanged() {
  const el = ldSelEl(); if (!el) return;
  el.bold = !!chk('ld-insp-bold'); ldMarkDirty(); ldRender(); ldSchedulePreview();
}
function ldInspUpperChanged() {
  const el = ldSelEl(); if (!el) return;
  el.uppercase = !!chk('ld-insp-upper'); ldMarkDirty(); ldSchedulePreview();
}
function ldInspOutlineChanged() {
  const el = ldSelEl(); if (!el) return;
  el.outline = !!chk('ld-insp-outline'); ldMarkDirty(); ldRender(); ldSchedulePreview();
}
function ldInspIconChanged() {
  const el = ldSelEl(); if (!el) return;
  const sel = document.getElementById('ld-insp-icon');
  el.text = sel ? sel.value : '';
  ldMarkDirty(); ldRender(); ldSchedulePreview();
}
function ldInspQrKindChanged() {
  const el = ldSelEl(); if (!el) return;
  const sel = document.getElementById('ld-insp-qr-kind');
  el.qr_kind = sel ? sel.value : 'url';
  ldMarkDirty(); ldRenderInspector(); ldSchedulePreview();
}
function ldInspQrNoteChanged() {
  const el = ldSelEl(); if (!el) return;
  const node = document.getElementById('ld-insp-qr-note');
  el.qr_extra = node ? node.value : '';
  ldMarkDirty(); ldSchedulePreview();
}
function ldInspSizeChanged() {
  const el = ldSelEl(); if (!el) return;
  el.size_scale = parseFloat(val('ld-insp-size')) || 1; ldMarkDirty(); ldSchedulePreview();
}
function ldInspTextChanged() {
  const el = ldSelEl(); if (!el) return;
  const node = document.getElementById('ld-insp-text');
  el.text = node ? node.value : '';
  ldMarkDirty();
  ldRender(); ldSchedulePreview();
  // Keep focus and caret in the text field after the re-render.
  const again = document.getElementById('ld-insp-text');
  if (again) { again.focus(); }
}
function ldDeleteSelected() {
  if (_ldSel < 0 || _ldSel >= _ldEls.length) return;
  _ldEls.splice(_ldSel, 1);
  _ldSel = -1;
  ldMarkDirty();
  ldRender(); ldSchedulePreview();
}

// -- Live preview -----------------------------------------------------------

function ldSchedulePreview() {
  clearTimeout(_ldPreviewTimer);
  _ldPreviewTimer = setTimeout(ldDoPreview, 300);
}

async function ldDoPreview() {
  const img = document.getElementById('ld-preview-img');
  const msg = document.getElementById('ld-preview-msg');
  if (!img) return;
  const layout = ldLayoutDict();
  if (!layout.elements.length) {
    if (msg) msg.innerHTML = '<span class="text-secondary">Add a field to see the label.</span>';
    if (img.dataset.url) { URL.revokeObjectURL(img.dataset.url); img.removeAttribute('data-url'); }
    img.removeAttribute('src');
    return;
  }
  try {
    const r = await fetch('printing/label/layout/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layout: layout }),
    });
    if (!r.ok) {
      if (msg) msg.innerHTML = '<span class="text-warning"><i class="bi bi-info-circle me-1"></i>That design could not be previewed. Adjust a field and try again.</span>';
      return;
    }
    const blob = await r.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url = URL.createObjectURL(blob);
    img.dataset.url = url;
    img.src = url;
    if (msg) msg.innerHTML = '';
  } catch (_) {
    if (msg) msg.innerHTML = '<span class="text-secondary">Preview is not available right now.</span>';
  }
}

// -- Save / reset -----------------------------------------------------------

async function ldSaveLayout(btn) {
  const el = document.getElementById('ld-save-result');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  if (el) el.innerHTML = '';
  try {
    const r = await fetch('printing/label/layout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layout: ldLayoutDict() }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || 'Could not save the design.');
    ldClearDirty();
    if (el) el.innerHTML = d.cleared
      ? '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Design cleared. Labels use the built-in layout.</span>'
      : '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Design saved. Your printed labels will use it.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function ldResetDefault(btn) {
  const el = document.getElementById('ld-save-result');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Resetting…';
  if (el) el.innerHTML = '';
  try {
    // Clearing the saved layout returns the print path to the built-in renderer.
    const r = await fetch('printing/label/layout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ layout: {} }),
    });
    await r.json().catch(() => ({}));
    _ldEls = LD_DEFAULT_ELS.map(e => Object.assign({}, e));
    _ldSel = -1;
    ldClearDirty();
    ldRender();
    ldSchedulePreview();
    if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Back to the built-in design.</span>';
  } catch (e) {
    if (el) el.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// -- Format presets ---------------------------------------------------------

async function ldFetchPresets() {
  const sel = document.getElementById('label_format');
  if (!sel) return;
  try {
    const r = await fetch('printing/label/presets');
    const d = await r.json();
    _ldPresets = (d && d.presets) || [];
    sel.innerHTML = '<option value="">Custom size</option>' +
      _ldPresets.map(p => `<option value="${p.key}">${p.name}</option>`).join('');
    ldMatchFormatToSize();
  } catch (_) { /* leave the Custom-only dropdown in place */ }
}

// -- Printer-reported sizes (FoodAssistant-u55y) -----------------------------
// A label printer advertises the media it actually supports; this pulls that
// list from the effective label queue via CUPS and offers it as one-tap
// picks, so setup does not require measuring label stock by hand.
async function ldFetchPrinterMedia() {
  const btn = document.getElementById('ld-printer-sizes-btn');
  const resultEl = document.getElementById('ld-printer-sizes-result');
  const listEl = document.getElementById('ld-printer-sizes-list');
  if (!btn || !listEl) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Checking printer...';
  if (resultEl) resultEl.innerHTML = '';
  listEl.innerHTML = '';
  try {
    const r = await fetch('printing/label-media');
    const d = await r.json();
    const sizes = (d && d.sizes) || [];
    if (!sizes.length) {
      if (resultEl) resultEl.innerHTML =
        '<span class="text-secondary">No sizes were reported by ' +
        (d && d.queue ? `"${d.queue}"` : 'your label printer') +
        '. Pick a format above or enter a size by hand.</span>';
      return;
    }
    listEl.innerHTML = sizes.map((s, i) =>
      `<button type="button" class="btn btn-outline-info btn-sm me-1 mb-1" onclick="ldUsePrinterSize(${i})">${s.label}</button>`
    ).join('');
    listEl.dataset.sizes = JSON.stringify(sizes);
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<span class="text-danger">${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// A pick fills the manual width/height fields (converted mm -> in, matching
// how the size is stored) and clears the format dropdown, same as any other
// manual size edit.
function ldUsePrinterSize(index) {
  const listEl = document.getElementById('ld-printer-sizes-list');
  if (!listEl || !listEl.dataset.sizes) return;
  const sizes = JSON.parse(listEl.dataset.sizes);
  const s = sizes[index];
  if (!s) return;
  const wIn = document.getElementById('label_width_in');
  const hIn = document.getElementById('label_height_in');
  if (wIn) wIn.value = Math.round((s.w_mm / 25.4) * 100) / 100;
  if (hIn) hIn.value = Math.round((s.h_mm / 25.4) * 100) / 100;
  ldOnSizeInput();
}

// Picking a format fills the size fields and seeds the designer with that
// preset's starting layout. "Custom size" leaves both the size and the design
// as the user has them.
function ldApplyPreset() {
  const sel = document.getElementById('label_format');
  if (!sel) return;
  const key = sel.value;
  if (!key) return;
  const p = _ldPresets.find(x => x.key === key);
  if (!p) return;

  const wIn = document.getElementById('label_width_in');
  const hIn = document.getElementById('label_height_in');
  const dIn = document.getElementById('label_dpi');
  if (wIn) wIn.value = p.width_in;
  // A square or round label stays 1:1, so the height follows the width; a
  // rectangle takes the preset's own height.
  if (hIn) hIn.value = ldShapeIsSquareSided() ? p.width_in : p.height_in;
  if (dIn && p.dpi) dIn.value = p.dpi;
  if (typeof refreshLabelPreview === 'function') refreshLabelPreview();
  if (typeof refreshDecorativePreview === 'function') refreshDecorativePreview();

  const layout = p.layout || {};
  const els = Array.isArray(layout.elements)
    ? layout.elements.map(ldNormalizeEl).filter(Boolean) : [];
  _ldEls = els.length ? els : LD_DEFAULT_ELS.map(e => Object.assign({}, e));
  _ldSel = -1;
  ldMarkDirty();
  ldRender();
  ldSchedulePreview();
}

// A manual size edit means the stock is no longer a named format, and the canvas
// aspect and preview should follow the new size. The stock size travels with
// the saved layout (ldLayoutDict embeds it), so this counts as a design change.
function ldOnSizeInput() {
  const sel = document.getElementById('label_format');
  if (sel) sel.value = '';
  // A square or round label keeps its two sides equal: mirror the width the
  // user just typed into the (disabled) height field so the saved stock is 1:1.
  if (ldShapeIsSquareSided()) {
    const hIn = document.getElementById('label_height_in');
    if (hIn) hIn.value = parseFloat(val('label_width_in')) || 2.0;
  }
  ldMarkDirty();
  ldRender();
  ldSchedulePreview();
}

// On load, pre-select the format whose size matches the saved stock (else
// Custom), so the dropdown reflects reality.
function ldMatchFormatToSize() {
  const sel = document.getElementById('label_format');
  if (!sel) return;
  const size = ldGetSize();
  const hit = _ldPresets.find(p =>
    Math.abs(p.width_in - size.w) < 0.001 && Math.abs(p.height_in - size.h) < 0.001);
  sel.value = hit ? hit.key : '';
}

// -- Icon picker --------------------------------------------------------

// Pull the curated icon list from the backend so the picker always matches
// services/label_render.py ICON_GLYPHS; the LD_ICONS seed above covers the
// first paint before this lands.
async function ldFetchIcons() {
  try {
    const r = await fetch('printing/decorative/icons');
    const d = await r.json();
    if (d && Array.isArray(d.icons) && d.icons.length) LD_ICONS = d.icons;
  } catch (_) { /* keep the seeded list */ }
  const sel = document.getElementById('ld-insp-icon');
  if (sel) {
    sel.innerHTML = LD_ICONS.map(i =>
      `<option value="${i.key}">${i.glyph} ${i.key}</option>`).join('');
    ldRenderInspector();
  }
}

// -- Saved layout presets (FoodAssistant-rhqa) -------------------------

// A small named library of designs, separate from the one "current" design
// Save layout stores. Lets a user keep a couple of label designs and switch
// between them.

async function ldFetchNamedPresets() {
  const sel = document.getElementById('ld-preset-select');
  if (!sel) return;
  try {
    const r = await fetch('printing/label/layout/presets');
    const d = await r.json();
    _ldNamedPresets = (d && d.presets) || [];
  } catch (_) {
    _ldNamedPresets = [];
  }
  sel.innerHTML = '<option value="">Choose a saved design…</option>' +
    _ldNamedPresets.map(p => `<option value="${p.name}">${p.name}</option>`).join('');
}

async function ldSaveAsPreset(btn) {
  const nameIn = document.getElementById('ld-preset-name');
  const result = document.getElementById('ld-preset-result');
  const name = nameIn ? nameIn.value.trim() : '';
  if (!name) {
    if (result) result.innerHTML = '<span class="text-warning">Give this design a name first.</span>';
    return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
  if (result) result.innerHTML = '';
  try {
    const r = await fetch('printing/label/layout/presets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, layout: ldLayoutDict() }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || 'Could not save that design.');
    _ldNamedPresets = d.presets || [];
    await ldFetchNamedPresets();
    const sel = document.getElementById('ld-preset-select');
    if (sel) sel.value = name;
    if (nameIn) nameIn.value = '';
    if (result) result.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved "${name}".</span>`;
  } catch (e) {
    if (result) result.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Loading a preset only changes the elements on the canvas; it does not save
// automatically (Save layout still applies it to printed labels), and it
// leaves the stock size alone since a saved design carries no size of its own.
function ldLoadPreset() {
  const sel = document.getElementById('ld-preset-select');
  const result = document.getElementById('ld-preset-result');
  if (!sel || !sel.value) return;
  const p = _ldNamedPresets.find(x => x.name === sel.value);
  if (!p) return;
  const layout = p.layout || {};
  const els = Array.isArray(layout.elements)
    ? layout.elements.map(ldNormalizeEl).filter(Boolean) : [];
  if (!els.length) {
    if (result) result.innerHTML = '<span class="text-warning">That saved design has no usable fields.</span>';
    return;
  }
  _ldEls = els;
  _ldSel = -1;
  ldMarkDirty();
  ldRender();
  ldSchedulePreview();
  if (result) result.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Loaded "${p.name}". Save layout to print with it.</span>`;
}

async function ldDeletePreset(btn) {
  const sel = document.getElementById('ld-preset-select');
  const result = document.getElementById('ld-preset-result');
  const name = sel ? sel.value : '';
  if (!name) {
    if (result) result.innerHTML = '<span class="text-warning">Choose a saved design to delete.</span>';
    return;
  }
  if (!window.confirm(`Delete the saved design "${name}"? This cannot be undone.`)) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Deleting…';
  try {
    const r = await fetch('printing/label/layout/presets/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name }),
    });
    const d = await r.json().catch(() => ({}));
    _ldNamedPresets = d.presets || [];
    await ldFetchNamedPresets();
    if (result) result.innerHTML = `<span class="text-success"><i class="bi bi-check-circle me-1"></i>Deleted "${name}".</span>`;
  } catch (e) {
    if (result) result.innerHTML = `<span class="text-danger"><i class="bi bi-x-circle-fill me-1"></i>${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
