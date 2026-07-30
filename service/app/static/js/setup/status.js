// Settings Status dashboard loader (FoodAssistant-w00b).
//
// The Status pane renders a row per subsystem with a placeholder "Checking…"
// pill (id "status-pill-<key>") and a detail line (id "status-detail-<key>").
// Opening the pane (and the Refresh button) calls loadStatusSummary(), which
// fetches /setup/status/summary and fills each pill and detail it finds. A
// failed fetch marks every existing row "Unknown" rather than leaving a blank
// page. Which rows exist is decided server-side by deployment mode, so this
// just fills whatever the template rendered.

// Map a summary state to the .set-pill CSS kind.
const _STATUS_PILL_KIND = {
  good: 'good',
  warn: 'warn',
  bad: 'bad',
  unknown: 'neutral',
};
// Short label shown inside each pill for a state.
const _STATUS_PILL_LABEL = {
  good: 'Healthy',
  warn: 'Attention',
  bad: 'Problem',
  unknown: 'Unknown',
};

function _setStatusPill(key, state, label, detail) {
  const pill = document.getElementById('status-pill-' + key);
  if (pill) {
    const kind = _STATUS_PILL_KIND[state] || 'neutral';
    pill.className = 'set-pill set-pill-' + kind;
    pill.textContent = label || _STATUS_PILL_LABEL[state] || 'Unknown';
  }
  const det = document.getElementById('status-detail-' + key);
  if (det && detail !== undefined) det.textContent = detail;
}

// Every row key the pane may render, so a failed fetch can mark them all.
function _statusRowKeys() {
  return Array.from(document.querySelectorAll('#pane-status [id^="status-pill-"]'))
    .map(function (el) { return el.id.replace('status-pill-', ''); });
}

async function loadStatusSummary() {
  const keys = _statusRowKeys();
  // Subtle loading state: reset each pill to a neutral "Checking…".
  keys.forEach(function (k) { _setStatusPill(k, 'unknown', 'Checking…', 'Checking…'); });
  try {
    const r = await fetch('setup/status/summary');
    const d = await r.json();
    const items = (d && d.items) || {};
    keys.forEach(function (k) {
      const it = items[k];
      if (it) {
        // A pill label from the state keeps the copy consistent; the raw
        // detail carries the specifics ("Connected to Wi-Fi (Home)").
        _setStatusPill(k, it.state, _STATUS_PILL_LABEL[it.state], it.detail || '');
      } else {
        _setStatusPill(k, 'unknown', 'Unknown', 'Status is unavailable.');
      }
    });
  } catch (e) {
    keys.forEach(function (k) {
      _setStatusPill(k, 'unknown', 'Unknown', 'Status could not be loaded.');
    });
  }
}
