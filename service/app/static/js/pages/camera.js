// Camera page (templates/camera.html).
  // Camera feeds rendered from app settings. Three display modes:
  //   - HLS (.m3u8 stream): a <video> element (native HLS where supported).
  //   - Home Assistant (and any snapshot-only camera): a refreshing <img> driven
  //     by the snapshot proxy. HA's MJPEG stream is unreliable inside an <img>
  //     (and the token cannot go in the URL), so a periodic still is the robust,
  //     proven path, the same one the Stream Deck uses.
  //   - Direct MJPEG (a manual stream URL): a plain <img> on the stream.
  // An onerror shows a friendly "unavailable" panel instead of a broken icon.
  const CAMERAS = JSON.parse(document.getElementById('camera-data').textContent).cameras;
  const videoEl = document.getElementById('cameraVideo');
  const imgEl = document.getElementById('cameraImg');
  const errEl = document.getElementById('cameraError');
  const errMsgEl = document.getElementById('cameraErrorMsg');
  // How often the refreshing-snapshot mode pulls a new still, in milliseconds.
  const SNAPSHOT_INTERVAL_MS = 1500;
  let snapTimer = null;
  let snapOk = false;

  function _stopSnapshot() {
    if (snapTimer) { clearInterval(snapTimer); snapTimer = null; }
  }

  function _hideAll() {
    videoEl.classList.add('d-none');
    imgEl.classList.add('d-none');
    errEl.classList.add('d-none');
  }

  function _showError(msg) {
    _hideAll();
    if (errMsgEl) {
      errMsgEl.textContent = msg || 'Camera unavailable. Check that the feed is reachable.';
    }
    errEl.classList.remove('d-none');
  }

  // Refresh a same-origin <img> straight from the snapshot proxy. The proxy is
  // same-origin, so there is no mixed-content or CORS issue and a bare <img src>
  // is the most reliable path (more so than fetch+blob inside the kiosk's
  // Chromium). The proxy adds the Home Assistant bearer token server-side. On
  // the first load failure we do one diagnostic fetch to surface the HTTP status.
  let snapSrc = '';
  function _startSnapshot(src) {
    snapOk = false;
    snapSrc = src;
    const tick = () => {
      const sep = src.includes('?') ? '&' : '?';
      imgEl.src = `${src}${sep}_=${Date.now()}`;
    };
    imgEl.classList.remove('d-none');
    errEl.classList.add('d-none');
    tick();
    snapTimer = setInterval(tick, SNAPSHOT_INTERVAL_MS);
  }

  async function _reportSnapshotError() {
    if (!snapSrc) { _showError(); return; }
    try {
      const sep = snapSrc.includes('?') ? '&' : '?';
      const res = await fetch(`${snapSrc}${sep}_=${Date.now()}`, {cache: 'no-store'});
      if (res.ok) return;  // it actually works now; leave the image alone
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) { /* not json */ }
      if (res.status === 404) {
        // Nothing to show: the camera was saved without a stream or snapshot.
        _showError('This camera has no picture set up yet. Add its stream or snapshot address in Settings, Cameras.');
      } else {
        // The server's reason already reads as a sentence ("Camera unreachable: ...").
        _showError(detail || `The camera did not send a picture (HTTP ${res.status}).`);
      }
    } catch (e) {
      _showError('Could not reach Pantry Raider to load the camera. Check the connection and try again.');
    }
  }

  function selectCamera(index) {
    const cam = CAMERAS[index];
    _stopSnapshot();
    if (!cam) { _showError(); return; }
    // Stop any previous img/video load before switching source.
    imgEl.removeAttribute('src');
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
    _hideAll();

    // Accept both the current shape (stream_src) and the older one (stream_url).
    const stream = (cam.stream_src || cam.stream_url || '').trim();

    if (stream && stream.toLowerCase().split('?')[0].endsWith('.m3u8')) {
      // Live HLS via <video>.
      videoEl.src = stream;
      videoEl.classList.remove('d-none');
      videoEl.play().catch(() => { /* autoplay may be blocked; controls remain */ });
    } else {
      // Everything else: refresh the snapshot through the proxy by index. The
      // proxy handles HA cameras (bearer token) and redirects manual cameras to
      // their own snapshot URL, so this one path works for every camera without
      // depending on which fields the server populated.
      _startSnapshot(`ui/camera/${index}/snapshot`);
    }

    // Update the switcher active state.
    document.querySelectorAll('#cameraSwitcher [data-cam-index]').forEach(btn => {
      btn.classList.toggle('active', parseInt(btn.dataset.camIndex, 10) === index);
    });
  }

  imgEl.addEventListener('load', () => { snapOk = true; errEl.classList.add('d-none'); });
  imgEl.addEventListener('error', () => {
    // Keep the last good frame on a transient miss; on the first failure fetch
    // the proxy once to show the real HTTP status. The direct (non-snapshot)
    // <img> modes have no snapSrc, so they fall back to the generic panel.
    if (snapOk) return;
    if (snapTimer) { _reportSnapshotError(); } else { _showError(); }
  });
  videoEl.addEventListener('error', () => { if (!snapTimer) _showError(); });

  // Start on the requested camera (the server resolved ?cam=index|name to an
  // index, defaulting to 0), so a Stream Deck camera key opens its own feed.
  const INITIAL_INDEX = JSON.parse(document.getElementById('camera-data').textContent).initial_index;
  if (CAMERAS.length) {
    selectCamera(INITIAL_INDEX >= 0 && INITIAL_INDEX < CAMERAS.length ? INITIAL_INDEX : 0);
  }
