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
};

// The order fields appear in the add-a-field palette.
const LD_PALETTE_ORDER = ['name', 'added', 'best_by', 'best_by_date',
  'best_by_badge', 'extra', 'quantity', 'location', 'static', 'barcode', 'qr'];

// The shipped default food design, expressed as layout elements. Mirrors
// services/label_render.py default_food_layout: the element fractions are
// size-independent, so the same list seeds any stock size. Used by "Reset to
// default" and as a fallback when nothing is saved.
const LD_DEFAULT_ELS = [
  { field: 'name', x: 0, y: 0, w: 1, h: 0.40, align: 'left', bold: true, size_scale: 1, text: '', uppercase: false },
  { field: 'static', text: 'BEST BY', x: 0, y: 0.46, w: 0.6, h: 0.12, align: 'left', bold: true, size_scale: 1, uppercase: true },
  { field: 'best_by_badge', x: 0.6, y: 0.44, w: 0.4, h: 0.14, align: 'right', bold: false, size_scale: 1, text: '', uppercase: false },
  { field: 'best_by_date', x: 0, y: 0.58, w: 1, h: 0.24, align: 'left', bold: true, size_scale: 1, text: '', uppercase: false },
  { field: 'added', x: 0, y: 0.86, w: 1, h: 0.12, align: 'left', bold: false, size_scale: 1, text: '', uppercase: false },
];

// The white border kept clear on every side, in inches. Matches the render
// engine default so the schematic's margin guide lines up with the print.
const LD_MARGIN_IN = 0.06;

let _ldEls = [];          // current elements
let _ldSel = -1;          // selected element index (-1 = none)
let _ldInited = false;    // one-time setup guard
let _ldPresets = [];      // fetched format presets (with layouts)
let _ldPreviewTimer = null;
let _ldDrag = null;       // active drag/resize state

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
    ldRender();
    ldSchedulePreview();
    ldFetchPresets();
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

// The current stock size, read from the shared size fields.
function ldGetSize() {
  return {
    w: parseFloat(val('label_width_in')) || 2.0,
    h: parseFloat(val('label_height_in')) || 1.0,
    dpi: parseInt(val('label_dpi'), 10) || 203,
  };
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
    align: (field === 'name' || field === 'qr' || field === 'barcode') ? 'center' : 'left',
    bold: (field === 'name'), size_scale: 1, text: '', uppercase: false,
  };
  if (field === 'static') el.text = 'Text';
  _ldEls.push(el);
  _ldSel = _ldEls.length - 1;
  ldRender();
  ldSchedulePreview();
}

// -- Canvas rendering -------------------------------------------------------

// A readable label for a box: a custom-text box shows its text.
function ldBoxLabel(el) {
  if (el.field === 'static') return el.text || 'Custom text';
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
    textIn.placeholder = el.field === 'qr' ? 'QR text (blank uses the name)'
      : el.field === 'barcode' ? 'Barcode value (blank uses the name)'
        : 'Text to print';
  }

  insp.querySelectorAll('[data-align]').forEach(b =>
    b.classList.toggle('active', b.dataset.align === el.align));
  const size = document.getElementById('ld-insp-size');
  if (size) size.value = el.size_scale;
  const bold = document.getElementById('ld-insp-bold');
  if (bold) bold.checked = !!el.bold;
  const upper = document.getElementById('ld-insp-upper');
  if (upper) upper.checked = !!el.uppercase;
}

function ldSetAlign(a) {
  const el = ldSelEl(); if (!el) return;
  el.align = a; ldRender(); ldSchedulePreview();
}
function ldInspBoldChanged() {
  const el = ldSelEl(); if (!el) return;
  el.bold = !!chk('ld-insp-bold'); ldRender(); ldSchedulePreview();
}
function ldInspUpperChanged() {
  const el = ldSelEl(); if (!el) return;
  el.uppercase = !!chk('ld-insp-upper'); ldSchedulePreview();
}
function ldInspSizeChanged() {
  const el = ldSelEl(); if (!el) return;
  el.size_scale = parseFloat(val('ld-insp-size')) || 1; ldSchedulePreview();
}
function ldInspTextChanged() {
  const el = ldSelEl(); if (!el) return;
  const node = document.getElementById('ld-insp-text');
  el.text = node ? node.value : '';
  ldRender(); ldSchedulePreview();
  // Keep focus and caret in the text field after the re-render.
  const again = document.getElementById('ld-insp-text');
  if (again) { again.focus(); }
}
function ldDeleteSelected() {
  if (_ldSel < 0 || _ldSel >= _ldEls.length) return;
  _ldEls.splice(_ldSel, 1);
  _ldSel = -1;
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
  if (hIn) hIn.value = p.height_in;
  if (dIn && p.dpi) dIn.value = p.dpi;
  if (typeof refreshLabelPreview === 'function') refreshLabelPreview();
  if (typeof refreshDecorativePreview === 'function') refreshDecorativePreview();

  const layout = p.layout || {};
  const els = Array.isArray(layout.elements)
    ? layout.elements.map(ldNormalizeEl).filter(Boolean) : [];
  _ldEls = els.length ? els : LD_DEFAULT_ELS.map(e => Object.assign({}, e));
  _ldSel = -1;
  ldRender();
  ldSchedulePreview();
}

// A manual size edit means the stock is no longer a named format, and the canvas
// aspect and preview should follow the new size.
function ldOnSizeInput() {
  const sel = document.getElementById('label_format');
  if (sel) sel.value = '';
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
