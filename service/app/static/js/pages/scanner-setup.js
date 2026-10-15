// Scanner setup page (templates/scanner_setup.html).

// Reveal the sequence one step at a time. The final card (index == count) is
// the "all set" confirmation, so Next stops there and Back walks it in reverse.
(function () {
  var steps = Array.prototype.slice.call(document.querySelectorAll('#scan-steps .scan-step'));
  var last = steps.length - 1;
  var cur = 0;
  function show(i) {
    cur = Math.max(0, Math.min(last, i));
    steps.forEach(function (el, idx) { el.classList.toggle('active', idx === cur); });
    document.getElementById('scan-back').disabled = (cur === 0);
    var next = document.getElementById('scan-next');
    next.classList.toggle('d-none', cur === last);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  window.scanStep = function (delta) { show(cur + delta); };
  window.scanGoto = function (i) { show(i); };
  show(0);
})();

// The model picker reloads the page for the chosen scanner.
function pickScannerModel(model) {
  if (model) window.location.search = '?model=' + encodeURIComponent(model);
}
