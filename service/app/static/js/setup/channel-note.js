// Settings > Updates: the one-time note under the update channel picker.
// Dismissing it is remembered in this browser.

(function () {
  var note = document.getElementById('update-channel-default-note');
  if (!note) return;
  try {
    if (localStorage.getItem('prChannelNoteSeen') === '1') { note.remove(); return; }
  } catch (e) { /* no storage: keep showing it */ }
  var close = note.querySelector('button');
  if (close) close.addEventListener('click', function () {
    try { localStorage.setItem('prChannelNoteSeen', '1'); } catch (e) { }
    note.remove();
  });
})();
