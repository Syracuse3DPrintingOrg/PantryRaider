// Floating timer chips (FoodAssistant-kfda, successor to the timer window of
// FoodAssistant-8uqy).
//
// One small overlay chip per running server-side timer (GET /timers), shown on
// every page so a countdown started anywhere in the house stays visible while
// browsing. Tapping a chip opens the full Timers page. The chips only appear
// while at least one timer is running and disappear when none remain.
//
// Visibility is resolved server-side (the timer_chips setting: on / off / auto,
// where auto hides the chips at large or extra-large interface scale) and
// arrives as data-chips-hidden on the container. A hidden device exits here
// before starting any polling, so it costs nothing.
//
// Two clocks, like the server: we POLL /timers every few seconds to learn which
// timers exist and to pick up new/cancelled ones, but between polls we TICK each
// visible countdown locally once a second from deadline_epoch minus the
// browser's own clock (Date.now()). That keeps the mm:ss display smooth without
// hammering the server, and matches the server's shareable-countdown contract:
// remaining = deadline_epoch - now, clamped at zero, expired once it hits zero.
// A hidden tab skips the network trip (visibilitychange resyncs on return).
(function () {
  var POLL_MS = 5000;   // how often we re-ask the server which timers exist (chips tick locally between polls)
  var TICK_MS = 1000;   // how often we redraw the local countdowns

  function start() {
    var wrap = document.getElementById('timerChips');
    if (!wrap) return;
    if (wrap.getAttribute('data-chips-hidden') === '1') return;
    // The Timers page draws every timer full-size with its own poll; floating
    // chips there were pure duplication, doubling GET /timers on an idle kiosk
    // (FoodAssistant-7dt9).
    if (document.getElementById('timersGrid')) return;

    var timers = [];

    // Audible timer chime (FoodAssistant-soj1, carried over from the timer
    // window). Quiet mode silences it so a finished timer is signalled only by
    // the highlighted chip. We chime once per timer, the first render it is
    // seen expired, tracked by id in `chimed`. Synthesised with the Web Audio
    // API so there is no asset to ship; some browsers gate audio until the page
    // has had a user gesture, which a kiosk gets from its normal taps.
    var quiet = document.documentElement.getAttribute('data-quiet-mode') === 'true';
    var chimed = {};
    function chime() {
      if (quiet) return;
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        var ctx = new Ctx();
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = 880;
        osc.connect(gain);
        gain.connect(ctx.destination);
        gain.gain.setValueAtTime(0.0001, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.6);
        osc.start();
        osc.stop(ctx.currentTime + 0.6);
        osc.onended = function () { try { ctx.close(); } catch (e) { } };
      } catch (e) { /* audio unavailable: stay visual only */ }
    }

    function fmt(remaining) {
      var total = Math.max(0, Math.floor(remaining));
      var m = Math.floor(total / 60);
      var s = total % 60;
      return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }

    // Compute remaining from the absolute epoch deadline and the browser clock,
    // falling back to the server-provided remaining_seconds if no deadline.
    function remainingOf(t) {
      if (typeof t.deadline_epoch === 'number') {
        return t.deadline_epoch - (Date.now() / 1000);
      }
      var rs = (typeof t.remaining_seconds === 'number') ? t.remaining_seconds
             : (typeof t.seconds === 'number') ? t.seconds : 0;
      return rs;
    }

    function render() {
      if (!timers.length) {
        wrap.classList.add('d-none');
        wrap.innerHTML = '';
        return;
      }
      wrap.classList.remove('d-none');
      wrap.innerHTML = '';
      for (var i = 0; i < timers.length; i++) {
        var t = timers[i];
        var remaining = remainingOf(t);
        var expired = remaining <= 0;

        if (expired) {
          var key = String(t.id != null ? t.id : t.label);
          if (!chimed[key]) { chimed[key] = true; chime(); }
        }

        // <base href> makes 'ui/timers' resolve on every page, ingress included.
        var chip = document.createElement('a');
        chip.className = 'timer-chip' + (expired ? ' timer-chip-expired' : '');
        chip.href = 'ui/timers';
        chip.title = 'Open timers';

        var label = document.createElement('span');
        label.className = 'timer-chip-label';
        label.textContent = t.label || ('Timer ' + (t.id != null ? t.id : (i + 1)));

        var clock = document.createElement('span');
        clock.className = 'timer-chip-clock';
        clock.textContent = expired ? 'done' : fmt(remaining);

        chip.appendChild(label);
        chip.appendChild(clock);
        wrap.appendChild(chip);
      }
    }

    // Fold a fresh timer list into the chips: only running/expired timers show,
    // and chimes are forgotten for timers the server dropped so a reused id
    // chimes again. Shared by the consolidated-poll subscription and the legacy
    // fetch below.
    function applyRows(rows) {
      rows = Array.isArray(rows) ? rows : [];
      // Only display running timers; an expired one is shown highlighted until
      // the server drops it, but a cancelled timer just disappears.
      timers = rows.filter(function (t) { return t && (t.running || t.expired); });
      var present = {};
      timers.forEach(function (t) { present[String(t.id != null ? t.id : t.label)] = 1; });
      Object.keys(chimed).forEach(function (k) { if (!present[k]) delete chimed[k]; });
      render();
    }

    function poll() {
      // A hidden tab skips the network trip; the local render tick keeps the
      // visible countdowns correct from deadline_epoch, and the chime still
      // fires on time because expiry is computed locally too.
      if (document.hidden) return;
      fetch('timers', { cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { applyRows(data && data.timers); })
        .catch(function () { /* empty or unreachable: leave last state */ });
    }

    setInterval(render, TICK_MS);  // smooth local countdown between polls (local only)

    // Prefer the consolidated kiosk poll (one request feeds every surface);
    // fall back to the dedicated /timers poll on a page or cached copy without
    // the shared loop.
    if (window.PRKioskStatus) {
      window.PRKioskStatus.subscribe(function (s) {
        if (s && s.timers && Array.isArray(s.timers.timers)) applyRows(s.timers.timers);
      }, { interval: POLL_MS });
    } else {
      poll();
      setInterval(poll, POLL_MS);
      // Coming back to the tab resyncs right away instead of waiting a cycle.
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) poll();
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
