// Demo-mode banner (templates/_demo_banner.html): while it shows, the body
// carries demo-banner-on and --pr-demo-banner-h holds the banner's measured
// height, so the fixed navbar and the page move down by exactly that much.
// The banner wraps onto more lines on a narrow phone, which is why the height
// is measured rather than assumed. Dismissing the banner takes the room back.
(function () {
  var banner = document.getElementById('demoBanner');
  if (!banner) return;
  var root = document.documentElement;

  function layout() {
    var shown = false;
    try {
      shown = document.body.contains(banner) && window.getComputedStyle(banner).display !== 'none';
    } catch (e) { shown = document.body.contains(banner); }
    var h = shown ? banner.offsetHeight : 0;
    root.style.setProperty('--pr-demo-banner-h', h + 'px');
    document.body.classList.toggle('demo-banner-on', shown);
  }

  layout();
  if (window.ResizeObserver) {
    try { new ResizeObserver(layout).observe(banner); } catch (e) { /* resize listener below covers it */ }
  }
  window.addEventListener('resize', layout);
  document.addEventListener('DOMContentLoaded', layout);
  window.addEventListener('load', layout);
  banner.addEventListener('closed.bs.alert', layout);
  banner.addEventListener('close.bs.alert', function () {
    document.body.classList.remove('demo-banner-on');
    root.style.setProperty('--pr-demo-banner-h', '0px');
  });

  // A change the demo refused (the server marks it with X-Demo-Read-Only)
  // brings the banner back and pulses it, so a button that has no error
  // message of its own still tells the visitor why nothing happened.
  function remind() {
    if (!document.body.contains(banner)) {
      banner.classList.add('show');
      document.body.insertBefore(banner, document.body.firstChild);
      layout();
    }
    banner.classList.remove('pr-demo-pulse');
    void banner.offsetWidth;
    banner.classList.add('pr-demo-pulse');
  }
  if (window.fetch) {
    var realFetch = window.fetch;
    window.fetch = function () {
      return realFetch.apply(this, arguments).then(function (r) {
        try {
          if (r.status === 403 && r.headers.get('X-Demo-Read-Only') === '1') remind();
        } catch (e) { /* never let the reminder break the caller */ }
        return r;
      });
    };
  }
})();
