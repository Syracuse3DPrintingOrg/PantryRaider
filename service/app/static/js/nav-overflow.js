// Top nav row overflow (base.html). The row holds every top-level page, which
// does not fit beside the brand and the utility icons on a laptop-width
// window, and its scrollbar is hidden, so the last tabs used to sit clipped
// off the right edge with no sign they were there. Measure the row and hide
// the tabs that do not fit; the More menu lists every page, so each one
// stays a click away, and the More button is marked while it holds tabs the
// row could not show. Runs on load, on resize and when the row's size
// changes (a badge count, a web font arriving).
(function () {
  var row = document.querySelector('.navbar .nav-primary');
  if (!row) return;
  var more = document.getElementById('moreMenu');
  var HIDDEN = 'pr-nav-overflow';
  var pending = false;

  function items() {
    return Array.prototype.slice.call(row.children).filter(function (li) {
      return li.classList && li.classList.contains('nav-item');
    });
  }

  function layout() {
    pending = false;
    var list = items();
    for (var i = 0; i < list.length; i++) list[i].classList.remove(HIDDEN);
    var hidden = [];
    // A row that is not shown (phone, the kiosk's narrow panel) has nothing
    // to measure: the hamburger and the floating nav carry navigation there.
    var shown = false;
    try { shown = window.getComputedStyle(row).display !== 'none' && row.clientWidth > 0; } catch (e) { shown = false; }
    if (shown && row.scrollWidth > row.clientWidth + 1) {
      // Hide from the end until the row fits, leaving the current page's tab
      // for last so it stays on the bar whenever it can.
      var order = list.slice().reverse();
      for (var j = 0; j < order.length; j++) {
        if (order[j].querySelector('.nav-link.active')) {
          order.push(order.splice(j, 1)[0]);
          break;
        }
      }
      for (var k = 0; k < order.length && row.scrollWidth > row.clientWidth + 1; k++) {
        order[k].classList.add(HIDDEN);
        hidden.push(order[k]);
      }
    }
    if (more) more.classList.toggle('pr-more-has-tabs', hidden.length > 0);
  }

  function schedule() {
    if (pending) return;
    pending = true;
    (window.requestAnimationFrame || setTimeout)(layout);
  }

  layout();
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(schedule, function () {});
  }
  if (window.ResizeObserver) {
    try {
      var ro = new ResizeObserver(schedule);
      ro.observe(row);
      if (row.parentElement) ro.observe(row.parentElement);
    } catch (e) { /* the resize listener covers it */ }
  }
})();
