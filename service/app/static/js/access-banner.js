// Access banners (templates/_access_banner.html): per-row dismissal
// remembered in this browser, plus the layout that keeps a fixed banner
// clear of the navbar and the NeoKey key rail.

(function () {
  var root = document.getElementById('pr-access-banner');
  if (!root) return;
  var fixed = root.classList.contains('pr-access-banner-fixed');
  var DAY_MS = 24 * 60 * 60 * 1000;

  function readUntil(key) {
    try {
      return Number(window.localStorage.getItem(key)) || 0;
    } catch (e) {
      return 0;
    }
  }
  function writeUntil(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (e) { /* storage blocked: the notice comes back next page */ }
  }

  // The NeoKey key rail (kiosk.css, Add page only) is fixed to the right edge
  // above the navbar. When it is there, keep the banner's text and Dismiss
  // button to its left, like the navbar's own controls.
  function railInset() {
    if (!fixed) return 0;
    var rail = document.getElementById('modeSelector');
    if (!rail || !document.documentElement.classList.contains('pr-neokey')) return 0;
    try {
      if (window.getComputedStyle(rail).position !== 'fixed') return 0;
      var left = rail.getBoundingClientRect().left;
      var gap = window.innerWidth - left;
      return gap > 0 && gap < window.innerWidth / 2 ? Math.ceil(gap) + 6 : 0;
    } catch (e) {
      return 0;
    }
  }

  function layout() {
    var inset = railInset();
    root.classList.toggle('pr-access-banner-railed', inset > 0);
    root.style.paddingRight = inset > 0 ? inset + 'px' : '';
    var rows = root.querySelectorAll('.pr-access-row');
    var shown = false;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].style.display !== 'none') shown = true;
    }
    root.style.display = shown ? '' : 'none';
    var h = shown ? root.offsetHeight : 0;
    document.documentElement.style.setProperty('--pr-access-banner-h', h + 'px');
    document.documentElement.classList.toggle('pr-access-banner-shown', shown);
    if (fixed && document.body) document.body.classList.toggle('access-banner-on', shown);
  }

  var rows = root.querySelectorAll('[data-pr-dismiss-key]');
  for (var i = 0; i < rows.length; i++) {
    (function (row) {
      var key = row.getAttribute('data-pr-dismiss-key');
      var days = Number(row.getAttribute('data-pr-dismiss-days')) || 1;
      if (readUntil(key) > Date.now()) row.style.display = 'none';
      var btn = row.querySelector('.btn-close');
      if (btn) btn.addEventListener('click', function () {
        writeUntil(key, Date.now() + days * DAY_MS);
        row.style.display = 'none';
        layout();
      });
    })(rows[i]);
  }

  layout();
  if (window.ResizeObserver) {
    try { new ResizeObserver(layout).observe(root); } catch (e) { /* resize listener below covers it */ }
  }
  window.addEventListener('resize', layout);
  // Measure again once the rest of the page (and the kiosk stylesheet, which
  // loads on its own) is in place.
  document.addEventListener('DOMContentLoaded', layout);
  window.addEventListener('load', layout);
})();
