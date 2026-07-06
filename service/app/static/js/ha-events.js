/*
 * On-screen Home Assistant event channel (Pantry Raider).
 *
 * When enabled (Settings > Home Assistant), the page polls /events/poll for
 * events pushed from Home Assistant and shows them on the display:
 *   - notification -> a toast in the top-right, coloured by level
 *   - camera       -> a full-screen pop-up of a camera feed for a few seconds
 *
 * On load we read the current last id first, so a page that opens long after an
 * event was sent does not replay a backlog; we only show events that arrive
 * after the page connects. Pure vanilla JS with self-injected styles, so it has
 * no dependency on the rest of the page.
 */
(function () {
  var cfg = document.getElementById('ha-events-config');
  if (!cfg) return;
  // Per-device override (FoodAssistant-vcuz): a device can show or hide on-screen
  // events independently of the server default. localStorage 'haEventsShow' is
  // '1' (show), '0' (hide), or absent (follow the server default).
  var ov = null;
  try { ov = localStorage.getItem('haEventsShow'); } catch (e) { }
  var enabled = ov === '1' ? true : (ov === '0' ? false : cfg.dataset.default === '1');
  if (!enabled) return;

  var POLL_MS = 4000;
  var DEFAULT_TOAST_MS = 8000;
  var lastId = 0;
  var camTimer = null;
  var camSnapTimer = null;

  // --- styles + containers -------------------------------------------------
  var style = document.createElement('style');
  style.textContent =
    '.hae-toasts{position:fixed;top:64px;right:14px;z-index:2050;display:flex;flex-direction:column;gap:10px;max-width:360px}' +
    '.hae-toast{background:#1f242b;border:1px solid #333b45;border-left:5px solid #14b8c4;border-radius:10px;' +
    'padding:10px 36px 10px 12px;color:#e6e9ed;box-shadow:0 .4rem 1rem rgba(0,0,0,.4);position:relative;' +
    'opacity:0;transform:translateX(16px);transition:opacity .25s,transform .25s}' +
    '.hae-toast.show{opacity:1;transform:none}' +
    '.hae-toast .hae-title{font-weight:700;margin-bottom:2px}' +
    '.hae-toast .hae-x{position:absolute;top:6px;right:8px;cursor:pointer;color:#9aa4af;border:0;background:none;font-size:18px;line-height:1}' +
    '.hae-toast.info{border-left-color:#14b8c4}.hae-toast.success{border-left-color:#2ea043}' +
    '.hae-toast.warning{border-left-color:#d29922}.hae-toast.error{border-left-color:#da3633}' +
    '.hae-cam{position:fixed;inset:0;z-index:2060;background:rgba(0,0,0,.92);display:flex;flex-direction:column;' +
    'align-items:center;justify-content:center;gap:12px}' +
    '.hae-cam img{max-width:96vw;max-height:82vh;object-fit:contain;background:#000;border-radius:8px}' +
    '.hae-cam .hae-cap{color:#e6e9ed;font-size:18px}' +
    '.hae-cam .hae-close{position:absolute;top:14px;right:18px;color:#fff;background:rgba(0,0,0,.5);border:0;' +
    'border-radius:8px;font-size:22px;padding:4px 12px;cursor:pointer}';
  document.head.appendChild(style);

  var toasts = document.createElement('div');
  toasts.className = 'hae-toasts';
  document.body.appendChild(toasts);

  // --- notification toast ---------------------------------------------------
  function showToast(ev) {
    var el = document.createElement('div');
    el.className = 'hae-toast ' + (ev.level || 'info');
    var title = ev.title ? '<div class="hae-title"></div>' : '';
    el.innerHTML = title + '<div class="hae-msg"></div><button class="hae-x" aria-label="Dismiss">&times;</button>';
    if (ev.title) el.querySelector('.hae-title').textContent = ev.title;
    el.querySelector('.hae-msg').textContent = ev.message || '';
    el.querySelector('.hae-x').onclick = function () { dismiss(el); };
    toasts.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('show'); });
    var ms = (ev.timeout > 0 ? ev.timeout * 1000 : DEFAULT_TOAST_MS);
    setTimeout(function () { dismiss(el); }, ms);
  }
  function dismiss(el) {
    el.classList.remove('show');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
  }

  // --- camera pop-up --------------------------------------------------------
  function closeCamera() {
    if (camTimer) { clearTimeout(camTimer); camTimer = null; }
    if (camSnapTimer) { clearInterval(camSnapTimer); camSnapTimer = null; }
    var ex = document.querySelector('.hae-cam');
    if (ex && ex.parentNode) ex.parentNode.removeChild(ex);
  }
  function showCamera(ev) {
    if (!ev.src) return;
    closeCamera();
    var box = document.createElement('div');
    box.className = 'hae-cam';
    box.innerHTML = '<button class="hae-close" aria-label="Close">&times;</button>' +
      '<img alt="Camera"><div class="hae-cap"></div>';
    box.querySelector('.hae-cap').textContent = ev.name || 'Camera';
    box.querySelector('.hae-close').onclick = closeCamera;
    var img = box.querySelector('img');
    function refresh() {
      var sep = ev.src.indexOf('?') >= 0 ? '&' : '?';
      img.src = ev.src + sep + '_=' + Date.now();
    }
    document.body.appendChild(box);
    refresh();
    camSnapTimer = setInterval(refresh, 1500);
    var secs = ev.seconds > 0 ? ev.seconds : 20;
    camTimer = setTimeout(closeCamera, secs * 1000);
  }

  // --- navigate (HA drives the on-screen page) -----------------------------
  function doNavigate(ev) {
    var p = String(ev.path || '').replace(/^\/+/, '');
    if (!p) return;
    // Same-origin relative paths only: never follow a scheme or a
    // protocol-relative URL the server should already have refused.
    if (/^[a-z][a-z0-9+.-]*:/i.test(p) || p.indexOf('//') === 0) return;
    // Already on this page: don't reload (avoids a refresh loop).
    var here = window.location.pathname.replace(/^\/+/, '');
    if (here === p || here === p.split('?')[0]) return;
    try { window.location.assign(p); } catch (e) { /* ignore */ }
  }

  function handle(ev) {
    if (ev.type === 'notification') showToast(ev);
    else if (ev.type === 'camera') showCamera(ev);
    else if (ev.type === 'navigate') doNavigate(ev);
  }

  // --- poll loop ------------------------------------------------------------
  function poll() {
    // A hidden tab has no screen to pop toasts or camera views on: skip the
    // fetch and just reschedule. Events are delivered by id (since=lastId),
    // so anything that arrives while hidden still shows on the next visible
    // poll rather than being lost.
    if (document.hidden) {
      setTimeout(poll, POLL_MS);
      return;
    }
    fetch('events/poll?since=' + lastId, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d) {
          if (typeof d.last_id === 'number') lastId = Math.max(lastId, d.last_id);
          (d.events || []).forEach(handle);
        }
      })
      .catch(function () { /* offline: try again next tick */ })
      .finally(function () { setTimeout(poll, POLL_MS); });
  }

  // Read the current last id first so we don't replay a backlog, then poll.
  fetch('events/poll?since=999999999', { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d && typeof d.last_id === 'number') lastId = d.last_id; })
    .catch(function () {})
    .finally(function () { setTimeout(poll, POLL_MS); });
})();
