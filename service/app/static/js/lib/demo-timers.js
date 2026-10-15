// Finished timers on the public demo (FoodAssistant-fy6kj).
//
// Timers are shared by every screen in the house, so dismissing one deletes it
// for everybody, and the demo refuses that like any other change. Left alone,
// a finished timer sat on every demo page as a "done" chip for a day, with a
// Dismiss button that did nothing. Here a visitor's dismiss hides the timer in
// this browser tab only, and a timer that finished more than LINGER_S seconds
// ago stops showing on its own. Nothing is written to the server.
//
// Loaded only when demo mode is on (base.html), so a normal install never runs
// it: the timer chips and the Timers page check for window.PRDemoTimers.
(function () {
  var LINGER_S = 60;
  var STORE = 'prDemoHiddenTimers';
  var hidden = {};

  try {
    var saved = JSON.parse(sessionStorage.getItem(STORE) || '{}');
    if (saved && typeof saved === 'object') hidden = saved;
  } catch (e) { /* storage blocked: keep the list in memory for this page */ }

  // A timer id can be reused after the server drops one, so the deadline is
  // part of the key and a new timer with an old id still shows.
  function key(t) {
    return String(t.id != null ? t.id : t.label) + '@' + String(t.deadline_epoch);
  }

  function hide(t) {
    if (!t) return;
    hidden[key(t)] = 1;
    try { sessionStorage.setItem(STORE, JSON.stringify(hidden)); } catch (e) { }
  }

  // The timers this tab should still show.
  function visible(rows) {
    var now = Date.now() / 1000;
    return (rows || []).filter(function (t) {
      if (!t || hidden[key(t)]) return false;
      if (typeof t.deadline_epoch === 'number' && now - t.deadline_epoch > LINGER_S) return false;
      return true;
    });
  }

  // True when the server turned a change down because this is the demo.
  function refused(resp) {
    try {
      return !!resp && resp.status === 403 && resp.headers.get('X-Demo-Read-Only') === '1';
    } catch (e) { return false; }
  }

  window.PRDemoTimers = { LINGER_S: LINGER_S, hide: hide, visible: visible, refused: refused };
})();
