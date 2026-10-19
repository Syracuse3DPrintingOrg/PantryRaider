// Shared fullscreen scanner. Include _camera_barcode_scanner.html once per page.
// Requires Bootstrap and the PR/pageStorageGet helpers provided by base.html.
// start({title, onScan, onError}), stop(), showToast(html, type, title),
// queueResult(html, type, title), loadDecoder() (also used by barcode photos).
// Notification HTML must escape external text with PR.esc; titles are plain text.
(function () {
  const PR = window.PR;
  const cameraScanModal = document.getElementById('cameraScanModal');
  const cameraSoundButton = document.getElementById('cameraSoundBtn');
  const cameraVibrationButton = document.getElementById('cameraVibrationBtn');
  const cameraTorchButton = document.getElementById('cameraTorchBtn');
  const cameraScanPulse = document.getElementById('cameraScanPulse');
  const cameraLookupToast = document.getElementById('cameraLookupToast');
  const lookupToastQueue = [];
  let cameraSession = null;
  let decoderPromise = null;

  // The 367 KB decoder is shared by live scanning and photos, but loaded only on demand.
  function loadDecoder() {
    if (window.Html5Qrcode) return Promise.resolve();
    if (!decoderPromise) {
      decoderPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = cameraScanModal.dataset.decoderSrc;
        script.onload = resolve;
        script.onerror = () => {
          decoderPromise = null;
          script.remove();
          reject(new Error('scanner library unavailable'));
        };
        document.head.appendChild(script);
      });
    }
    return decoderPromise;
  }

  setCameraToggle(cameraSoundButton, pageStorageGet(cameraSoundButton.id) === 'true');
  setCameraToggle(cameraVibrationButton, pageStorageGet(cameraVibrationButton.id) !== 'false');
  cameraSoundButton.addEventListener('click', () => toggleCameraFeedback(cameraSoundButton));
  cameraVibrationButton.addEventListener('click', () => toggleCameraFeedback(cameraVibrationButton));
  cameraTorchButton.addEventListener('click', toggleCameraTorch);
  cameraLookupToast.addEventListener('hidden.bs.toast', showNextLookupToast);
  cameraScanModal.addEventListener('hidden.bs.modal', stopScanner);
  window.addEventListener('pagehide', stopScanner);
  window.addEventListener('resize', fitCameraViewport);
  window.visualViewport?.addEventListener('resize', fitCameraViewport);
  window.visualViewport?.addEventListener('scroll', fitCameraViewport);

  function fitCameraViewport() {
    if (!cameraSession || cameraSession.stopped) return;
    // Fixed-position CSS uses the layout viewport, which can exceed the visible phone screen.
    const viewport = window.visualViewport;
    cameraScanModal.style.width = (viewport ? viewport.width : window.innerWidth) + 'px';
    cameraScanModal.style.height = (viewport ? viewport.height : window.innerHeight) + 'px';
    cameraScanModal.style.left = (viewport ? viewport.offsetLeft : 0) + 'px';
    cameraScanModal.style.top = (viewport ? viewport.offsetTop : 0) + 'px';
  }

  function showCameraToast(html, type, title) {
    if (!cameraSession || cameraSession.stopped) return;
    const toast = document.getElementById('cameraScanToast');
    toast.dataset.level = type;
    document.getElementById('cameraScanToastTitle').textContent = title ||
      (type === 'info' ? 'Barcode recognized' : type === 'success' ? 'Scan complete' : 'Scan needs attention');
    document.getElementById('cameraScanToastMessage').innerHTML = html;
    bootstrap.Toast.getOrCreateInstance(toast, { delay: 5000 }).show();
  }

  function queueResult(html, type, title) {
    if (!cameraSession || cameraSession.stopped) return;
    lookupToastQueue.push({ html, type, title });
    if (!cameraLookupToast.classList.contains('show')) showNextLookupToast();
  }

  function showNextLookupToast() {
    if (!cameraSession || cameraSession.stopped || !lookupToastQueue.length) return;
    const result = lookupToastQueue.shift();
    cameraLookupToast.dataset.level = result.type;
    document.getElementById('cameraLookupToastTitle').textContent = result.title;
    document.getElementById('cameraLookupToastMessage').innerHTML = result.html;
    // A separate, sequential toast keeps late results from replacing feedback for the current scan.
    bootstrap.Toast.getOrCreateInstance(cameraLookupToast, { delay: 4000, animation: false }).show();
  }

  function setCameraToggle(button, enabled) {
    button.setAttribute('aria-pressed', String(enabled));
    button.classList.toggle('active', enabled);
    button.title = button.getAttribute('aria-label') + (enabled ? ': on' : ': off');
  }

  function setCameraSupported(button, supported) {
    button.disabled = !supported;
    button.classList.toggle('unsupported', !supported);
    button.setAttribute('aria-describedby', supported ? '' : button.id + 'Unsupported');
    if (!supported) {
      setCameraToggle(button, false);
      button.title = button.getAttribute('aria-label') + ' is not supported by this ' +
        (button === cameraTorchButton ? 'camera or browser' : 'browser');
    }
  }

  function closeCameraAudio(session) {
    try { session.audio?.close().catch(() => {}); } catch (e) { /* optional audio */ }
    session.audio = null;
  }

  async function prepareCameraAudio() {
    const session = cameraSession;
    if (!session || session.stopped || cameraSoundButton.getAttribute('aria-pressed') !== 'true') return;
    try {
      // Create/resume during Start Camera or the sound-toggle tap, not a later decode callback.
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!session.audio) session.audio = new Audio();
      await session.audio.resume();
    } catch (e) {
      if (session.stopped) return;
      closeCameraAudio(session);
      setCameraToggle(cameraSoundButton, false);
      showCameraToast('Sound could not be enabled. Visual feedback is still available.', 'warning', 'Sound unavailable');
    }
  }

  function toggleCameraFeedback(button) {
    const session = cameraSession;
    if (!session || session.stopped || button.disabled) return;
    const enabled = button.getAttribute('aria-pressed') !== 'true';
    setCameraToggle(button, enabled);
    try { localStorage.setItem(button.id, String(enabled)); } catch (e) { /* keep the choice for this page */ }
    if (button === cameraSoundButton) {
      if (enabled) prepareCameraAudio();
      else closeCameraAudio(session);
    } else if (!enabled) {
      try { navigator.vibrate?.(0); } catch (e) { /* optional haptics */ }
    }
  }

  function signalCameraScan(session) {
    clearTimeout(session.pulseTimer);
    cameraScanPulse.classList.remove('scan-pulse');
    void cameraScanPulse.offsetWidth;
    cameraScanPulse.classList.add('scan-pulse');
    session.pulseTimer = setTimeout(() => cameraScanPulse.classList.remove('scan-pulse'), 500);
    try {
      if (cameraVibrationButton.getAttribute('aria-pressed') === 'true') navigator.vibrate?.(150);
    } catch (e) { /* optional haptics must not interrupt handling the barcode */ }
    try {
      const audio = session.audio;
      if (cameraSoundButton.getAttribute('aria-pressed') !== 'true' || audio?.state !== 'running') return;
      const oscillator = audio.createOscillator(), gain = audio.createGain();
      oscillator.frequency.value = 880;
      oscillator.connect(gain);
      gain.connect(audio.destination);
      gain.gain.setValueAtTime(0.0001, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.15, audio.currentTime + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.12);
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
      oscillator.start();
      oscillator.stop(audio.currentTime + 0.12);
    } catch (e) { /* optional audio must not interrupt handling the barcode */ }
  }

  async function toggleCameraTorch() {
    const session = cameraSession;
    if (!session || session.stopped || cameraTorchButton.disabled || cameraTorchButton.classList.contains('d-none')) return;
    const enabled = cameraTorchButton.getAttribute('aria-pressed') !== 'true';
    cameraTorchButton.disabled = true;
    try {
      await session.decoder.applyVideoConstraints({ advanced: [{ torch: enabled }] });
      if (session.stopped) return;
      const actual = session.decoder.getRunningTrackSettings().torch;
      if (typeof actual === 'boolean' && actual !== enabled) throw new Error('Torch setting was ignored');
      setCameraToggle(cameraTorchButton, enabled);
    } catch (e) {
      if (!session.stopped) {
        showCameraToast('The camera could not switch its flashlight. You can keep scanning or try again.', 'warning', 'Flashlight unavailable');
      }
    } finally {
      if (!session.stopped) cameraTorchButton.disabled = false;
    }
  }

  async function startScanner({ title = 'Camera Scanner', onScan, onError }) {
    if (cameraSession) return;
    const session = cameraSession = {
      decoder: null, start: null, stopped: false,
      scrollX: window.scrollX, scrollY: window.scrollY,
    };
    // Fix the background in place as overflow:hidden alone still allows mobile page panning.
    document.documentElement.style.setProperty('--camera-scroll-top', -session.scrollY + 'px');
    document.documentElement.classList.add('camera-scanning');
    fitCameraViewport();
    document.getElementById('cameraScanTitle').textContent = title;
    document.getElementById('cameraScanLoading').classList.remove('d-none');
    bootstrap.Modal.getOrCreateInstance(cameraScanModal).show();
    for (const [button, supported] of [
      [cameraSoundButton, typeof (window.AudioContext || window.webkitAudioContext) === 'function'],
      [cameraVibrationButton, typeof navigator.vibrate === 'function'],
    ]) {
      setCameraSupported(button, supported);
    }
    prepareCameraAudio();
    try {
      await loadDecoder();
    } catch (e) {
      if (session.stopped) return;
      await stopScanner();
      onError('The camera scanner could not load.', 'warning');
      return;
    }
    if (session.stopped) return;
    try {
      // Product barcodes only (plus QR): cheaper frames and fewer misreads.
      const decoder = session.decoder = new Html5Qrcode('camera-reader', {
        formatsToSupport: [
          Html5QrcodeSupportedFormats.EAN_13, Html5QrcodeSupportedFormats.EAN_8,
          Html5QrcodeSupportedFormats.UPC_A, Html5QrcodeSupportedFormats.UPC_E,
          Html5QrcodeSupportedFormats.CODE_128, Html5QrcodeSupportedFormats.CODE_39,
          Html5QrcodeSupportedFormats.QR_CODE,
        ],
        verbose: false,
      });
      let lastCode = '';
      let lastSeen = 0;
      session.start = decoder.start(
        { facingMode: 'environment' },
        {
          fps: 10,
          qrbox: (width, height) => ({ width: Math.min(280, width), height: Math.min(120, height) }),
          experimentalFeatures: { useBarCodeDetectorIfSupported: true },
          // Keep the sharp feed and continuous focus; match the full-screen view
          // through camera constraints, not CSS cropping that misaligns decoding.
          videoConstraints: {
            facingMode: 'environment',
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            aspectRatio: { ideal: cameraScanModal.clientWidth / cameraScanModal.clientHeight },
            advanced: [{ focusMode: 'continuous' }],
          },
        },
        (decodedText) => {
          if (session.stopped) return;
          const code = decodedText.trim();
          if (!code) return;
          const now = Date.now();
          // Keep extending the guard while a barcode stays in view, not just
          // for a fixed cooldown after saving (which would count it repeatedly).
          const repeated = code === lastCode && now - lastSeen < 1500;
          lastCode = code;
          lastSeen = now;
          if (repeated) return;
          signalCameraScan(session);
          showCameraToast(PR.esc(code), 'info');
          onScan(code);
        },
        () => {}
      );
      await session.start;
      if (session.stopped) return;
      document.getElementById('cameraScanLoading').classList.add('d-none');
      setCameraSupported(cameraTorchButton, false);
      cameraTorchButton.classList.remove('d-none');
      // Where supported, continuous focus and 2x zoom help close-up labels.
      try {
        const caps = decoder.getRunningTrackCapabilities() || {};
        if (caps.torch === true || (Array.isArray(caps.torch) && caps.torch.includes(true) && caps.torch.includes(false))) {
          setCameraToggle(cameraTorchButton, !!decoder.getRunningTrackSettings().torch);
          setCameraSupported(cameraTorchButton, true);
        }
        const tweak = {};
        if (Array.isArray(caps.focusMode) && caps.focusMode.includes('continuous')) {
          tweak.focusMode = 'continuous';
        }
        if (caps.zoom && caps.zoom.max >= 2) {
          tweak.zoom = Math.min(2, caps.zoom.max);
        }
        if (Object.keys(tweak).length) {
          decoder.applyVideoConstraints({ advanced: [tweak] }).catch(() => {});
        }
      } catch (e) { /* capability probing is best-effort */ }
    } catch (err) {
      if (session.stopped) return;
      await stopScanner();
      onError('Camera error: ' + String(err), 'danger');
    }
  }

  async function stopScanner() {
    const session = cameraSession;
    if (!session || session.stopped) return;
    session.stopped = true;
    clearTimeout(session.pulseTimer);
    cameraScanPulse.classList.remove('scan-pulse');
    closeCameraAudio(session);
    try { navigator.vibrate?.(0); } catch (e) { /* optional haptics */ }
    cameraTorchButton.classList.add('d-none');
    setCameraToggle(cameraTorchButton, false);
    bootstrap.Modal.getInstance(cameraScanModal).hide();
    bootstrap.Toast.getInstance(document.getElementById('cameraScanToast'))?.hide();
    lookupToastQueue.length = 0;
    bootstrap.Toast.getInstance(cameraLookupToast)?.hide();
    document.documentElement.classList.remove('camera-scanning');
    document.documentElement.style.removeProperty('--camera-scroll-top');
    window.scrollTo({ left: session.scrollX, top: session.scrollY, behavior: 'instant' });
    try {
      // Closing while permission is pending must also stop a late camera start.
      if (session.start) {
        await session.start;
        await session.decoder.stop();
      }
    } catch (e) { /* startup may have failed before the decoder could stop */ }
    finally {
      const reader = document.getElementById('camera-reader');
      const video = reader.querySelector('video');
      if (video && video.srcObject) video.srcObject.getTracks().forEach(track => track.stop());
      reader.replaceChildren();
      cameraSession = null;
    }
  }

  PR.cameraBarcodeScanner = {
    start: startScanner,
    stop: stopScanner,
    showToast: showCameraToast,
    queueResult,
    loadDecoder,
  };
})();
