// Consolidated kiosk status poll (FoodAssistant-us1i).
//
// One chained-setTimeout loop that hits GET kiosk/status and hands the merged
// answer to every on-glass surface that used to run its own poll: the floating
// timer chips, the Home Assistant event toasts, the nav badge counts, the
// scanner-mode tabs, the presence indicator, the system-health triangle, the
// kiosk hand-off and touch-calibration flags, and the Start page tiles. Each
// surface keeps its own LOCAL render ticks (the 1s countdowns never touch the
// network); only the fetches collapse onto this one request, cutting an idle
// kiosk from about eight polls per interval down to one.
//
// Consumer contract:
//   window.PRKioskStatus.subscribe(fn, {interval, wants})
//     fn(status) runs on every successful poll with the full response object.
//     interval is this surface's fastest acceptable cadence in ms (the loop
//     runs at the smallest interval any subscriber asks for, floored at 2s).
//     wants is an optional list of opt-in expensive fields; currently only
//     'expiring' (the Grocy-backed count the Start page needs). Returns an
//     unsubscribe function.
//   window.PRKioskStatus.last()    -> the most recent status object, or null.
//   window.PRKioskStatus.refresh() -> poll again shortly (after a local action).
//
// A status field is present ONLY when it could be gathered, so every consumer
// must guard on presence (if (status.timers) ...) and otherwise leave its last
// state. That is how an omitted admin-only field (a remote viewer session) and
// a blipping main server (a satellite whose forward failed) both read: nothing
// changes on the glass rather than everything blanking.
//
// Poll hygiene matches the sibling pollers this replaces: chained setTimeout
// (never setInterval, so a slow answer cannot stack requests), no fetch while
// the tab is hidden, exponential backoff to 30s on failure, and a snap back to
// the fast cadence on a good answer or when the tab becomes visible again.
(function () {
  if (window.PRKioskStatus) return;  // idempotent: define once

  var DEFAULT_MS = 4000;
  var MIN_MS = 2000;         // never poll faster than any single surface needs
  var BACKOFF_MAX_MS = 30000;

  var subs = [];             // {cb, interval, wants}
  var latest = null;
  var since = null;          // null => prime the HA-events cursor (no backlog replay)
  var delay = DEFAULT_MS;
  var started = false;
  var timer = null;
  var inFlight = false;      // a /kiosk/status fetch is currently in flight

  function effectiveInterval() {
    // The loop runs at the SMALLEST cadence any subscriber asks for, so a page
    // whose surfaces all want a slow cadence (the Start page's 15s tiles) polls
    // slowly, while a base.html page with a 4s toast surface polls at 4s. The
    // default only applies when nobody stated an interval.
    var ms = null;
    for (var i = 0; i < subs.length; i++) {
      var iv = subs[i].interval;
      // A subscriber may pass a function so its cadence can change with state,
      // e.g. the presence indicator asks for 2s only while its sensor is
      // firing and the slow backoff otherwise, so a sensor-less kiosk never
      // pins the whole poll at 2s.
      if (typeof iv === 'function') { try { iv = iv(); } catch (e) { iv = undefined; } }
      if (typeof iv === 'number' && iv > 0) ms = (ms === null) ? iv : Math.min(ms, iv);
    }
    if (ms === null) ms = DEFAULT_MS;
    return Math.max(MIN_MS, ms);
  }

  function wantFlags() {
    var wants = {};
    for (var i = 0; i < subs.length; i++) {
      var w = subs[i].wants;
      if (w) for (var j = 0; j < w.length; j++) wants[w[j]] = 1;
    }
    return wants;
  }

  function buildUrl() {
    // Relative URL like the other kiosk pollers, so a <base href> ingress
    // prefix survives; never a leading slash.
    var q = 'since=' + (since === null ? 999999999 : since);
    try {
      if (localStorage.getItem('kioskMode') === 'true') q += '&kiosk=1';
    } catch (e) { /* storage blocked: treat as a non-kiosk caller */ }
    if (wantFlags().expiring) q += '&expiring=1';
    return 'kiosk/status?' + q;
  }

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  function dispatch(data) {
    latest = data;
    // Advance the shared HA-events cursor so the next poll only carries newer
    // events; the toast surface just shows whatever arrives each tick.
    if (data && data.events && typeof data.events.last_id === 'number') {
      since = data.events.last_id;
    }
    for (var i = 0; i < subs.length; i++) {
      try { subs[i].cb(data); } catch (e) { /* one bad surface never stalls the rest */ }
    }
  }

  function poll() {
    // Never run two fetches at once: schedule(0) from refresh() or the
    // visibilitychange handler clears only the pending timer, not an in-flight
    // request, so without this guard a rapid toggle could start a second
    // concurrent poll. The in-flight request's finally reschedules, so nothing
    // stalls.
    if (inFlight) return;
    // A hidden tab has nothing on screen to update: skip the trip and just
    // reschedule (visibilitychange resyncs on return). Nothing to do with no
    // subscribers either.
    if (document.hidden || !subs.length) { schedule(effectiveInterval()); return; }
    inFlight = true;
    fetch(buildUrl(), { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data) { delay = effectiveInterval(); dispatch(data); }
        else { delay = Math.min(delay * 2, BACKOFF_MAX_MS); }
      })
      .catch(function () { delay = Math.min(delay * 2, BACKOFF_MAX_MS); })
      .finally(function () { inFlight = false; schedule(delay); });
  }

  function ensureStarted() {
    if (started) return;
    started = true;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', poll);
    } else {
      poll();
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { delay = effectiveInterval(); schedule(0); }
    });
  }

  window.PRKioskStatus = {
    subscribe: function (cb, opts) {
      if (typeof cb !== 'function') return function () {};
      opts = opts || {};
      var entry = { cb: cb, interval: opts.interval, wants: opts.wants };
      subs.push(entry);
      // A late subscriber paints from the last answer at once instead of
      // waiting a whole cycle.
      if (latest) { try { cb(latest); } catch (e) { /* ignore */ } }
      ensureStarted();
      return function () {
        var i = subs.indexOf(entry);
        if (i >= 0) subs.splice(i, 1);
      };
    },
    last: function () { return latest; },
    refresh: function () { if (started) schedule(0); },
  };
})();
