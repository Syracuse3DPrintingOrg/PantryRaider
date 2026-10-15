// Touchscreen calibration page (templates/calibrate.html).

(function () {
  // The touch panel also presents as a mouse (the ADS7846 has a mouse0
  // handler), so every tap fires a browser click at the UNCALIBRATED position.
  // During the corner sequence those stray clicks would land on the Cancel
  // button and abort calibration (FoodAssistant-mox4). Swallow all click-type
  // events on the page so nothing is triggered by the panel's mouse emulation;
  // the tap sampling runs entirely off the SSE stream, not browser input.
  ['click', 'auxclick', 'contextmenu', 'dblclick'].forEach(function (t) {
    document.addEventListener(t, function (e) {
      e.preventDefault();
      e.stopPropagation();
    }, true);
  });
  // Targets are inset from the edges so the crosshair stays fully on screen.
  const CORNERS = [
    { label: 'top-left',     tx: 0.05, ty: 0.05 },
    { label: 'top-right',    tx: 0.95, ty: 0.05 },
    { label: 'bottom-right', tx: 0.95, ty: 0.95 },
    { label: 'bottom-left',  tx: 0.05, ty: 0.95 },
  ];

  // Calibration is a plain 4-corner affine fit straight from the raw device
  // coordinates to the VISIBLE target positions, at whatever rotation the
  // display is currently in. A 2x3 affine already represents any rotation,
  // flip, or axis swap between the panel and the screen, so the four taps
  // capture the full mapping without a separate rotation step. (An earlier
  // version tried to pre-compensate for a compositor output transform; on this
  // hardware that corrupted 90/180/270, so the straight fit is used instead.)

  let es = null;
  let step = 0;
  let samples = [];
  let ranges = null;

  const chEl = document.getElementById('crosshair');
  const msgEl = document.getElementById('msg');

  function _solve3(A, b) {
    const M = A.map((row, i) => [...row, b[i]]);
    for (let i = 0; i < 3; i++) {
      let p = i;
      for (let r = i + 1; r < 3; r++) if (Math.abs(M[r][i]) > Math.abs(M[p][i])) p = r;
      [M[i], M[p]] = [M[p], M[i]];
      for (let r = 0; r < 3; r++) {
        if (r === i) continue;
        const f = M[r][i] / M[i][i];
        for (let c = i; c <= 3; c++) M[r][c] -= f * M[i][c];
      }
    }
    return M.map((row, i) => row[3] / row[i]);
  }

  function solveAffine(s) {
    let sxx = 0, sxy = 0, sx = 0, syy = 0, sy = 0, n = 0;
    let bx0 = 0, bx1 = 0, bx2 = 0, by0 = 0, by1 = 0, by2 = 0;
    for (const [nx, ny, tx, ty] of s) {
      sxx += nx * nx; sxy += nx * ny; sx += nx;
      syy += ny * ny; sy += ny; n++;
      bx0 += nx * tx; bx1 += ny * tx; bx2 += tx;
      by0 += nx * ty; by1 += ny * ty; by2 += ty;
    }
    const A = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]];
    return [..._solve3(A, [bx0, bx1, bx2]), ..._solve3(A, [by0, by1, by2])];
  }

  function showCorner(idx) {
    const c = CORNERS[idx];
    chEl.style.left = (c.tx * 100) + '%';
    chEl.style.top  = (c.ty * 100) + '%';
    chEl.style.display = 'block';
    msgEl.textContent = 'Tap the crosshair (' + c.label + ')  -  ' +
                        (idx + 1) + ' of ' + CORNERS.length;
  }

  let finished = false;
  window.finishCalibration = function () {
    if (finished) return;
    finished = true;
    if (es) { es.close(); es = null; }
    // Return to the kiosk UI.
    window.location.href = '../../../ui/?kiosk=1';
  };

  // Cancellation lives on the REMOTE browser that started this, since an
  // uncalibrated panel is hard to tap accurately (FoodAssistant-mox4). Poll the
  // server for a remote cancel and leave when it is set. Esc still works for an
  // attached keyboard.
  (function () {
    // PR.poller: a slow answer never stacks a second request, and the request
    // is dropped when the page is left.
    const cancelPoll = setInterval(PR.poller(function (signal) {
      return fetch('cancel/pending', { cache: 'no-store', signal: signal })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.pending) { clearInterval(cancelPoll); window.finishCalibration(); } })
        .catch(function () { /* transient: keep polling */ });
    }), 1500);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') window.finishCalibration();
    });
  }());

  // Backstop: if no tap completes the sequence within two minutes, give up and
  // return to the UI rather than stranding the kiosk on a black screen.
  let idleTimer = setTimeout(window.finishCalibration, 120000);
  function resetIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(window.finishCalibration, 120000);
  }

  async function applyCalibration() {
    // Straight raw -> visible-target affine; the four corners capture the full
    // panel-to-screen mapping (including any rotation) on their own.
    const coeffs = solveAffine(samples);
    const matrix = coeffs.map(v => v.toFixed(4)).join(' ');
    msgEl.textContent = 'Applying calibration...';
    try {
      const r = await fetch('apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ matrix }),
      });
      const data = await r.json();
      if (data.ok) {
        // The host bridge schedules a detached kiosk restart a couple of
        // seconds out so this response lands first; the compositor then takes
        // this page down and relaunches at the app. Show the restarting state
        // meanwhile, and if the restart never materialises (or was skipped),
        // fall back to navigating back to the app ourselves.
        msgEl.textContent = 'Calibration saved. Restarting the display...';
        setTimeout(window.finishCalibration, data.kiosk_restarted ? 30000 : 2000);
      } else {
        msgEl.textContent = 'Apply failed: ' + (data.error || 'unknown error');
        setTimeout(window.finishCalibration, 3500);
      }
    } catch (e) {
      msgEl.textContent = 'Could not reach the app to apply that. Try again.';
      setTimeout(window.finishCalibration, 3500);
    }
  }

  function start() {
    es = new EventSource('events');
    es.onmessage = function (ev) {
      let d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.type === 'error') { msgEl.textContent = 'Error: ' + d.msg; return; }
      if (d.type === 'ranges') {
        ranges = d;
        showCorner(0);
        return;
      }
      if (d.type === 'tap' && ranges) {
        resetIdle();
        const xr = (ranges.x_max - ranges.x_min) || 1;
        const yr = (ranges.y_max - ranges.y_min) || 1;
        const nx = (d.x - ranges.x_min) / xr;
        const ny = (d.y - ranges.y_min) / yr;
        const c = CORNERS[step];
        // Fit to the VISIBLE target; the rotation is folded in at apply time.
        samples.push([nx, ny, c.tx, c.ty]);
        step++;
        if (step < CORNERS.length) {
          showCorner(step);
        } else {
          clearTimeout(idleTimer);
          es.close(); es = null;
          chEl.style.display = 'none';
          applyCalibration();
        }
      }
    };
    es.onerror = function () {
      msgEl.textContent = 'Touch stream disconnected. ' +
        'Make sure a touchscreen is connected, then reopen calibration.';
    };
  }

  start();
}());
