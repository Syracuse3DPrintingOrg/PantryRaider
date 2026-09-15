// Warn before leaving Settings with unsaved changes (FoodAssistant-1jzp). A real
// edit to any settings field marks the page dirty; any successful save clears it.
// We settle briefly first so the page's own init (which sets field values
// programmatically) does not look like a user edit.
(function () {
  var root = document.getElementById('settings-root');
  if (!root) return;
  var dirty = false;
  setTimeout(function () {
    root.addEventListener('input', function () { dirty = true; });
    root.addEventListener('change', function () { dirty = true; });
  }, 400);
  // Any successful POST to a settings-save endpoint means the page is saved.
  var SAVE_RE = /(setup\/save|setup\/cameras|setup\/ha\/|setup\/theme|setup\/scale|setup\/mode|setup\/storage-categories|setup\/update|admin\/logging)/;
  var _fetch = window.fetch;
  window.fetch = function (url, opts) {
    var p = _fetch.apply(this, arguments);
    try {
      var method = (opts && (opts.method || '')).toUpperCase();
      if (method === 'POST' && SAVE_RE.test(String(url || ''))) {
        p.then(function (r) { if (r && r.ok) dirty = false; }).catch(function () {});
      }
    } catch (e) { /* never let tracking break a real request */ }
    return p;
  };
  window.addEventListener('beforeunload', function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ''; return ''; }
  });
})();
