// Kitchen converter page (templates/convert.html).
(function () {
  // Conversion units grouped by category. Mass and volume use a linear factor
  // (value in the category base unit). Temperature is affine, so it carries
  // toBase/fromBase functions instead of a factor. Factors match the reference
  // tables shown on this page (1 oz = 28.35 g, 1 cup = 240 ml, and so on).
  var UNITS = [
    // Volume, base unit milliliter (ml)
    { id: "ml",    label: "Milliliter (ml)",   cat: "volume", factor: 1 },
    { id: "l",     label: "Liter (L)",         cat: "volume", factor: 1000 },
    { id: "tsp",   label: "Teaspoon (tsp)",    cat: "volume", factor: 5 },
    { id: "tbsp",  label: "Tablespoon (tbsp)", cat: "volume", factor: 15 },
    { id: "floz",  label: "Fluid ounce (fl oz)", cat: "volume", factor: 29.5735 },
    { id: "cup",   label: "Cup",               cat: "volume", factor: 240 },
    { id: "pint",  label: "Pint",              cat: "volume", factor: 473.176 },
    { id: "quart", label: "Quart",             cat: "volume", factor: 946.353 },
    { id: "gallon",label: "Gallon",            cat: "volume", factor: 3785.41 },
    // Weight, base unit gram (g)
    { id: "g",     label: "Gram (g)",          cat: "weight", factor: 1 },
    { id: "kg",    label: "Kilogram (kg)",     cat: "weight", factor: 1000 },
    { id: "oz",    label: "Ounce (oz)",        cat: "weight", factor: 28.3495 },
    { id: "lb",    label: "Pound (lb)",        cat: "weight", factor: 453.592 },
    // Temperature, affine
    { id: "c", label: "Celsius (°C)",    cat: "temp",
      toBase: function (c) { return c; },
      fromBase: function (c) { return c; } },
    { id: "f", label: "Fahrenheit (°F)", cat: "temp",
      toBase: function (f) { return (f - 32) * 5 / 9; },
      fromBase: function (c) { return c * 9 / 5 + 32; } },
    { id: "k", label: "Kelvin (K)",           cat: "temp",
      toBase: function (k) { return k - 273.15; },
      fromBase: function (c) { return c + 273.15; } }
  ];

  var CAT_LABEL = { volume: "Volume", weight: "Weight", temp: "Temperature" };

  function byId(id) { for (var i = 0; i < UNITS.length; i++) { if (UNITS[i].id === id) return UNITS[i]; } return null; }

  // Convert within a single category. Returns null across categories.
  function convert(value, from, to) {
    if (!from || !to || from.cat !== to.cat) return null;
    if (from.cat === "temp") {
      return to.fromBase(from.toBase(value));
    }
    return value * from.factor / to.factor;
  }

  function formatResult(n) {
    if (n === null || !isFinite(n)) return "–";
    // Trim to a sensible precision without trailing zeros.
    var rounded = Math.abs(n) >= 1000 ? n.toFixed(1) : n.toFixed(4);
    return parseFloat(rounded).toString();
  }

  var valueEl = document.getElementById("convValue");
  var fromEl = document.getElementById("convFrom");
  var toEl = document.getElementById("convTo");
  var resultEl = document.getElementById("convResult");
  var noteEl = document.getElementById("convNote");

  // Build grouped <option> lists for both selects.
  function buildOptions(sel) {
    var lastCat = null;
    var group = null;
    UNITS.forEach(function (u) {
      if (u.cat !== lastCat) {
        group = document.createElement("optgroup");
        group.label = CAT_LABEL[u.cat];
        sel.appendChild(group);
        lastCat = u.cat;
      }
      var opt = document.createElement("option");
      opt.value = u.id;
      opt.textContent = u.label;
      group.appendChild(opt);
    });
  }
  buildOptions(fromEl);
  buildOptions(toEl);
  fromEl.value = "cup";
  toEl.value = "ml";

  function recompute() {
    var from = byId(fromEl.value);
    var to = byId(toEl.value);
    var value = parseFloat(valueEl.value);
    if (isNaN(value)) {
      resultEl.textContent = "–";
      noteEl.textContent = "Enter a value to convert.";
      return;
    }
    if (from && to && from.cat !== to.cat) {
      resultEl.textContent = "–";
      noteEl.textContent = "Pick two units in the same category (" +
        CAT_LABEL[from.cat] + " vs " + CAT_LABEL[to.cat] + ").";
      return;
    }
    var out = convert(value, from, to);
    resultEl.textContent = formatResult(out) + " " + (to ? to.id : "");
    noteEl.textContent = formatResult(value) + " " + (from ? from.label : "") +
      " = " + formatResult(out) + " " + (to ? to.label : "");
  }

  fromEl.addEventListener("change", recompute);
  toEl.addEventListener("change", recompute);

  // On-screen numeric keypad. Edits a buffer, mirrored into the value field.
  var padDisplay = document.getElementById("convPadDisplay");
  var buffer = valueEl.value || "0";

  function renderPad() {
    padDisplay.textContent = buffer.length ? buffer : "0";
  }
  function commitBuffer() {
    valueEl.value = (buffer.length && buffer !== ".") ? buffer : "0";
    recompute();
  }
  function padPress(key) {
    if (key === "back") {
      buffer = buffer.slice(0, -1);
    } else if (key === ".") {
      if (buffer.indexOf(".") === -1) buffer += (buffer.length ? "." : "0.");
    } else {
      // Avoid a leading zero like "01".
      if (buffer === "0") buffer = key; else buffer += key;
    }
    renderPad();
    commitBuffer();
  }

  document.getElementById("convertKeypad").addEventListener("click", function (e) {
    var btn = e.target.closest("[data-key]");
    if (!btn) return;
    padPress(btn.getAttribute("data-key"));
  });
  document.getElementById("convPadClear").addEventListener("click", function () {
    buffer = "0";
    renderPad();
    commitBuffer();
  });

  // Sync the pad buffer from the field each time the keypad opens.
  var modalEl = document.getElementById("convKeypadModal");
  modalEl.addEventListener("show.bs.modal", function () {
    buffer = valueEl.value || "0";
    renderPad();
  });

  // Physical keyboard support when one is attached.
  modalEl.addEventListener("keydown", function (e) {
    if (e.key >= "0" && e.key <= "9") padPress(e.key);
    else if (e.key === ".") padPress(".");
    else if (e.key === "Backspace") padPress("back");
  });

  renderPad();
  recompute();
})();

// --- My conversions editor (device-local custom reference rows) -------------
function toggleConvEdit() {
  document.getElementById('convEdit').classList.toggle('d-none');
}
function addConvRow(label, value) {
  const wrap = document.getElementById('convEditRows');
  const div = document.createElement('div');
  div.className = 'row g-2 conv-edit-row';
  div.innerHTML =
    '<div class="col"><input class="form-control form-control-sm conv-label" placeholder="Label (e.g. 1 stick butter)"></div>' +
    '<div class="col"><input class="form-control form-control-sm conv-value" placeholder="Value (e.g. 113 g / 8 tbsp)"></div>' +
    '<div class="col-auto"><button class="btn btn-outline-danger btn-sm" type="button" aria-label="Remove this conversion" data-on-click="this.closest(\'.conv-edit-row\').remove()"><i class="bi bi-trash"></i></button></div>';
  div.querySelector('.conv-label').value = label || '';
  div.querySelector('.conv-value').value = value || '';
  wrap.appendChild(div);
}
async function saveConvRows(btn) {
  const rows = [];
  document.querySelectorAll('#convEditRows .conv-edit-row').forEach(r => {
    const label = r.querySelector('.conv-label')?.value.trim() || '';
    const value = r.querySelector('.conv-value')?.value.trim() || '';
    if (label || value) rows.push({label, value});
  });
  const el = document.getElementById('convSaveResult');
  if (btn) btn.disabled = true;
  try {
    await fetch('setup/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({convert_custom_rows: rows}),
    });
    if (el) el.innerHTML = '<span class="text-success">Saved. Reloading...</span>';
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    if (el) el.innerHTML = '<span class="text-danger">Could not save. ' + prReason(e) + '</span>';
    if (btn) btn.disabled = false;
  }
}
