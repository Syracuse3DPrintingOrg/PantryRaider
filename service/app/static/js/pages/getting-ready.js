// Getting ready page (templates/getting-ready.html): shows the first-start
// steps and hands off to setup when the stack is ready. data-kiosk on the
// script tag says whether this is the kiosk screen.

(function () {
  var me = document.currentScript;
  var KIOSK = !!me && me.getAttribute('data-kiosk') === '1';
  var box = document.getElementById('gr-steps');

  function icon(state) {
    if (state === 'done') return '<i class="bi bi-check-circle-fill text-success"></i>';
    if (state === 'working') return '<span class="spinner-border spinner-border-sm text-info"></span>';
    return '<i class="bi bi-circle text-secondary"></i>';
  }

  function render(steps) {
    if (!box || !steps || !steps.length) return;
    box.innerHTML = steps.map(function (s) {
      return '<div class="gr-step">' + icon(s.state) + '<span>' +
        String(s.label || '').replace(/[<>&"]/g, '') + '</span></div>';
    }).join('');
  }

  function handoff() {
    window.location.href = 'setup' + (KIOSK ? '?kiosk=1' : '');
  }

  async function poll() {
    try {
      var r = await fetch('ui/getting-ready/status', {cache: 'no-store'});
      var d = await r.json();
      if (d && d.steps) render(d.steps);
      if (d && d.ready) { handoff(); return; }
    } catch (e) { /* transient: keep polling */ }
    setTimeout(poll, 3000);
  }
  setTimeout(poll, 3000);
})();
