"""Continuous camera scanning with the real decoder and a synthetic video feed."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

# Only the hardware and native barcode detector are replaced. The vendored
# html5-qrcode, modal, toast, scan routing, and media cleanup run unchanged.
_CAMERA = """() => {
window.__camera = {
  code: '', streams: [], tracks: [], starts: 0, wait: false, fail: false,
  torchSupported: null, torchOn: false, torchCalls: [], torchFail: false, torchWait: false,
  vibrations: [], audioContexts: [], beeps: 0
};
navigator.vibrate = duration => { __camera.vibrations.push(duration); return true; };
const NativeAudioContext = window.AudioContext;
window.AudioContext = class extends NativeAudioContext {
  constructor() { super(); __camera.audioContexts.push(this); }
  createOscillator() {
    const oscillator = super.createOscillator(), start = oscillator.start.bind(oscillator);
    oscillator.start = (...args) => { __camera.beeps++; start(...args); };
    return oscillator;
  }
};
navigator.mediaDevices.getUserMedia = async (constraints) => {
  const state = window.__camera;
  state.starts++;
  if (state.wait) await new Promise(resolve => { state.release = resolve; });
  if (state.fail) throw new Error('Camera permission denied');
  const canvas = document.createElement('canvas');
  canvas.height = 720;
  canvas.width = Math.round(720 * constraints.video.aspectRatio.ideal);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#345';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const stream = canvas.captureStream(10);
  const frames = setInterval(() => ctx.fillRect(0, 0, canvas.width, canvas.height), 100);
  const track = stream.getVideoTracks()[0];
  const capabilities = track.getCapabilities.bind(track), settings = track.getSettings.bind(track);
  const stop = track.stop.bind(track);
  track.getCapabilities = () => ({...capabilities(),
    ...(state.torchSupported === null ? {} : {torch: state.torchSupported})});
  if (state.torchSupported === 'unavailable') track.getCapabilities = undefined;
  if (state.torchSupported === 'error') track.getCapabilities = () => { throw new Error('Capabilities unavailable'); };
  track.getSettings = () => ({...settings(), torch: state.torchOn});
  track.applyConstraints = async constraints => {
    const torch = constraints.advanced?.find(c => 'torch' in c)?.torch;
    if (torch === undefined) return;
    state.torchCalls.push(torch);
    if (state.torchWait) await new Promise(resolve => { state.releaseTorch = resolve; });
    if (state.torchFail || track.readyState === 'ended') throw new Error('Torch unavailable');
    if (!state.torchIgnore) state.torchOn = torch;
  };
  track.stop = () => { clearInterval(frames); state.torchOn = false; stop(); };
  state.tracks.push(track);
  state.streams.push(stream);
  return stream;
};
window.BarcodeDetector = class {
  static async getSupportedFormats() {
    return ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'qr_code'];
  }
  async detect() {
    return window.__camera.code
      ? [{rawValue: window.__camera.code, format: 'ean_13', boundingBox: {width: 200, height: 80}}] : [];
  }
};
}
"""


@pytest.fixture
def camera_page(browser, app_server):
    contexts = []

    def make(viewport=(390, 844), *, component_only=False):
        context = browser.new_context(
            viewport={'width': viewport[0], 'height': viewport[1]},
            is_mobile=True, has_touch=True,
            service_workers='block' if component_only else 'allow',
        )
        contexts.append(context)
        page = context.new_page()
        if component_only:
            page.route('**/static/js/manage-pantry.js*', lambda route: route.fulfill(
                body='', content_type='application/javascript'))
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(app_server + '/ui/add', wait_until='load')
        page.evaluate(_CAMERA)
        return page, errors

    yield make
    for context in contexts:
        context.close()


def _wait_for(page, expression):
    # Poll through the driver: wait_for_function uses eval, blocked by our CSP.
    def ready():
        return expression() if callable(expression) else page.evaluate(expression)

    for _ in range(100):
        if ready():
            return
        page.wait_for_timeout(50)
    assert ready(), expression


def _start(page):
    page.click('#startScanBtn')
    _wait_for(page, """() => {
      const video = document.querySelector('#camera-reader video');
      return video && !video.paused && video.readyState >= 2 && video.currentTime > 0
        && document.getElementById('cameraScanLoading').classList.contains('d-none');
    }""")


def _decode(page, code):
    page.evaluate('(code) => { window.__camera.code = code; }', code)


def _saved(route):
    code = route.request.post_data_json['barcode']
    route.fulfill(json={
        'status': 'queued',
        'item': {'id': int(code), 'barcode': code, 'name': 'Tea <special>'},
    })


def _wait_stopped(page):
    _wait_for(page, """document.getElementById('camera-reader').childElementCount === 0
      && __camera.tracks.every(t => t.readyState === 'ended')""")
    assert page.locator('#cameraScanModal').is_hidden()
    assert page.evaluate("!document.documentElement.classList.contains('camera-scanning')")
    assert page.locator('#cameraLookupToast').is_hidden()
    assert page.evaluate("__camera.tracks.every(t => t.readyState === 'ended')")
    assert page.locator('#cameraTorchBtn').is_hidden()
    assert not page.locator('#cameraScanPulse').evaluate("el => el.classList.contains('scan-pulse')")
    assert page.evaluate("__camera.audioContexts.every(ctx => ctx.state === 'closed')")
    assert not page.evaluate('__camera.torchOn')


def test_component_works_without_manage_pantry_script(camera_page):
    page, errors = camera_page(component_only=True)
    assert page.evaluate("typeof submitScan === 'undefined' && typeof cameraSession === 'undefined'")
    assert page.evaluate("typeof Html5Qrcode === 'undefined'")  # Still lazy-loaded.
    page.evaluate("""() => {
      __camera.codes = [];
      __camera.errors = [];
      const button = document.getElementById('startScanBtn');
      button.removeAttribute('data-on-click');
      button.addEventListener('click', () => PR.cameraBarcodeScanner.start({
        title: 'Standalone scanner',
        onScan: code => __camera.codes.push(code),
        onError: message => __camera.errors.push(message),
      }));
    }""")
    _start(page)
    assert page.locator('#cameraScanTitle').inner_text() == 'Standalone scanner'
    _decode(page, 'code <special>')
    _wait_for(page, '__camera.codes.length === 1')
    assert page.evaluate('__camera.codes') == ['code <special>']
    assert page.locator('#cameraScanToastMessage').inner_text() == 'code <special>'
    assert page.locator('#cameraScanToastMessage special').count() == 0
    page.evaluate("PR.cameraBarcodeScanner.showToast('Saved by the caller', 'success')")
    assert page.locator('#cameraScanToastMessage').inner_text() == 'Saved by the caller'
    page.evaluate("PR.cameraBarcodeScanner.queueResult('An older result', 'success', 'Earlier scan')")
    assert page.locator('#cameraLookupToastMessage').inner_text() == 'An older result'
    page.evaluate('PR.cameraBarcodeScanner.stop()')
    _wait_stopped(page)
    assert not page.evaluate('__camera.errors')
    assert not errors


def test_barcode_photo_uses_shared_lazy_decoder_without_starting_camera(camera_page):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    assert page.evaluate("typeof Html5Qrcode === 'undefined'")
    _decode(page, '1234567890123')
    page.evaluate("""async () => {
      const canvas = document.createElement('canvas');
      canvas.width = 320; canvas.height = 200;
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      const transfer = new DataTransfer();
      transfer.items.add(new File([blob], 'barcode.png', {type: 'image/png'}));
      const input = document.getElementById('barcodePhotoInput');
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {bubbles: true}));
    }""")
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    assert page.evaluate('__camera.starts') == 0
    assert page.evaluate('__camera.vibrations') == []
    assert page.locator('#cameraScanModal').is_hidden()
    _start(page)
    assert page.locator('script[src*="html5-qrcode.min.js"]').count() == 1
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_failed_decoder_load_allows_retry(camera_page):
    page, errors = camera_page()
    page.evaluate("""() => {
      const append = document.head.appendChild;
      document.head.appendChild = function(node) {
        if (!node.src?.includes('html5-qrcode.min.js')) return append.call(this, node);
        document.head.appendChild = append;
        queueMicrotask(() => node.dispatchEvent(new Event('error')));
        return node;
      };
    }""")
    page.click('#startScanBtn')
    _wait_for(page, "document.getElementById('barcode-status').textContent.includes('camera scanner could not load')")
    _wait_stopped(page)
    assert page.evaluate('__camera.starts') == 0
    _start(page)
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('viewport', [(390, 844), (844, 390), (1280, 800)])
def test_fullscreen_scans_continue_with_bottom_toast(camera_page, viewport):
    page, errors = camera_page(viewport)
    requests = []
    page.route('**/pending/scan', lambda route: requests.append(route))
    _start(page)
    bounds = page.locator('#cameraScanModal .modal-content').bounding_box()
    assert bounds == {'x': 0, 'y': 0, 'width': viewport[0], 'height': viewport[1]}
    video = page.locator('#camera-reader video').bounding_box()
    assert video['width'] == viewport[0]
    assert abs(video['height'] - viewport[1]) < 3

    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Barcode recognized'")
    assert '1234567890123' in page.locator('#cameraScanToastMessage').inner_text()
    page.wait_for_timeout(200)  # Let the toast's entrance transition finish.
    _wait_for(page, lambda: len(requests) == 1)
    toast = page.locator('#cameraScanToast').bounding_box()
    assert toast['y'] > viewport[1] - 200
    assert toast['y'] + toast['height'] <= viewport[1]
    assert requests[0].request.post_data_json == {
        'barcode': '1234567890123', 'quantity': 1, 'source': 'scanner',
    }
    _saved(requests[0])
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan complete'")
    assert 'Saved: Tea <special>' in page.locator('#cameraScanToastMessage').inner_text()
    assert page.locator('#cameraScanToastMessage special').count() == 0
    assert page.locator('#cameraScanModal').is_visible()
    assert page.evaluate("__camera.streams[0].getTracks()[0].readyState") == 'live'

    _decode(page, '1234567890124')
    _wait_for(page, "document.getElementById('cameraScanToastMessage').textContent === '1234567890124'")
    _wait_for(page, lambda: len(requests) == 2)
    _saved(requests[1])
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_camera_overlays_stay_on_screen_above_preview_with_large_text(camera_page):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate("document.documentElement.style.fontSize = '32px'; __camera.torchSupported = true")
    _start(page)
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan complete'")
    page.wait_for_timeout(200)
    close = page.locator('#stopScanBtn').bounding_box()
    assert close['x'] + close['width'] <= 390
    assert 44 <= close['width'] <= 60
    assert 0 <= close['y'] <= 50
    toast = page.locator('#cameraScanToast').bounding_box()
    assert 0 <= toast['x'] and toast['x'] + toast['width'] <= 390
    assert 0 <= toast['y'] and toast['y'] + toast['height'] <= 844

    # Decoder-owned elements cannot paint over the app's controls or feedback.
    page.locator('#camera-reader video').evaluate("""video => {
      video.style.position = 'relative';
      video.style.zIndex = '9999';
    }""")
    for selector in ('#stopScanBtn', '#cameraTorchBtn', '#cameraSoundBtn', '#cameraVibrationBtn', '#cameraScanToast'):
        assert page.locator(selector).evaluate("""el => {
          const r = el.getBoundingClientRect();
          return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
        }""")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('unsupported', [False, True])
def test_scanner_fits_visible_viewport_after_mobile_zoom_and_rotation(camera_page, unsupported):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate('__camera.torchSupported = true')
    if unsupported:
        page.evaluate('navigator.vibrate = window.AudioContext = window.webkitAudioContext = undefined; __camera.torchSupported = false')
    _start(page)
    cdp = page.context.new_cdp_session(page)
    for width, height, zoom, font_size in ((390, 844, 1.75, 16), (844, 390, 1.5, 16), (390, 667, 1, 32)):
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('(size) => { document.documentElement.style.fontSize = size + "px"; }', font_size)
        cdp.send('Emulation.setPageScaleFactor', {'pageScaleFactor': zoom})
        _wait_for(page, """() => {
          const v = visualViewport;
          const r = document.getElementById('cameraScanModal').getBoundingClientRect();
          return Math.abs(r.width - v.width) < 1 && Math.abs(r.height - v.height) < 1;
        }""")
        _decode(page, str(1234567890123 + width + height))
        _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan complete'")
        page.wait_for_timeout(200)
        for selector in ('#stopScanBtn', '#cameraTorchBtn', '#cameraSoundBtn', '#cameraVibrationBtn', '#cameraScanToast'):
            assert page.locator(selector).evaluate("""el => {
              const r = el.getBoundingClientRect(), v = visualViewport;
              return r.left >= v.offsetLeft && r.top >= v.offsetTop
                && r.right <= v.offsetLeft + v.width + 1
                && r.bottom <= v.offsetTop + v.height + 1;
            }""")
        for label in page.locator('.when-unsupported:visible').all():
            assert label.evaluate("""el => {
              const r = el.getBoundingClientRect(), button = el.closest('button').getBoundingClientRect();
              return r.left >= button.left && r.right <= button.right
                && r.top >= button.top && r.bottom <= button.bottom;
            }""")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_scanner_does_not_scroll_and_restores_page_position(camera_page):
    page, errors = camera_page((390, 600))
    page.locator('#startScanBtn').evaluate("el => el.scrollIntoView({block: 'start', behavior: 'instant'})")
    scroll_y = page.evaluate('window.scrollY')
    assert scroll_y > 0
    _start(page)
    assert page.evaluate("getComputedStyle(document.body).position") == 'fixed'
    assert page.locator('#cameraScanModal').evaluate("el => getComputedStyle(el).overflowY") == 'hidden'
    # A camera can ignore the requested aspect ratio; its tall feed must clip,
    # never make the modal or the page scroll.
    page.locator('#camera-reader video').evaluate("video => { video.style.height = '2000px'; }")
    cdp = page.context.new_cdp_session(page)
    before = page.evaluate('window.scrollY')
    cdp.send('Input.synthesizeScrollGesture', {
        'x': 150, 'y': 500, 'yDistance': -400, 'gestureSourceType': 'touch',
    })
    assert page.evaluate('window.scrollY') == before
    assert page.locator('#cameraScanModal').evaluate('el => el.scrollTop === 0 && el.scrollHeight === el.clientHeight')
    assert page.locator('#stopScanBtn').bounding_box()['y'] == 16
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert page.evaluate('window.scrollY') == scroll_y
    assert page.evaluate("getComputedStyle(document.body).position") != 'fixed'
    assert not errors


def test_held_barcode_only_counts_again_after_leaving_view(camera_page):
    page, errors = camera_page()
    codes = []

    def scan(route):
        codes.append(route.request.post_data_json['barcode'])
        _saved(route)

    page.route('**/pending/scan', scan)
    _start(page)
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan complete'")
    page.wait_for_timeout(2200)  # Longer than the guard, with continuous detections.
    assert codes == ['1234567890123']
    _decode(page, '')
    page.wait_for_timeout(1700)
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '2'")
    assert codes == ['1234567890123', '1234567890123']
    page.keyboard.press('Escape')
    _wait_stopped(page)
    assert page.locator('#startScanBtn').evaluate('(el) => el === document.activeElement')

    _start(page)  # A fresh session can scan the same item immediately.
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '3'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('reduced_motion', ['no-preference', 'reduce'])
def test_recognition_pulses_and_vibrates_once_with_sound_off(camera_page, reduced_motion):
    page, errors = camera_page()
    page.emulate_media(reduced_motion=reduced_motion)
    page.route('**/pending/scan', _saved)
    assert page.locator('#cameraSoundBtn').count() == 1
    _start(page)
    assert page.locator('#cameraSoundBtn').get_attribute('aria-pressed') == 'false'
    assert page.locator('#cameraVibrationBtn').get_attribute('aria-pressed') == 'true'
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    pulse = page.locator('#cameraScanPulse')
    assert pulse.evaluate("el => el.classList.contains('scan-pulse')")
    assert pulse.evaluate('el => getComputedStyle(el).pointerEvents') == 'none'
    if reduced_motion == 'reduce':
        assert pulse.evaluate('el => getComputedStyle(el).animationName') == 'none'
        assert pulse.evaluate('el => getComputedStyle(el).opacity') == '1'
    assert page.evaluate('__camera.vibrations') == [150]
    assert page.evaluate('__camera.audioContexts.length') == 0
    page.wait_for_timeout(1800)
    assert not pulse.evaluate("el => el.classList.contains('scan-pulse')")
    assert page.evaluate('__camera.vibrations') == [150]  # Holding a barcode does not buzz repeatedly.
    _decode(page, '1234567890124')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '2'")
    assert page.evaluate('__camera.vibrations') == [150, 150]
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_sound_and_vibration_toggles_are_independent_and_remembered(camera_page):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    _start(page)
    page.click('#cameraSoundBtn')
    page.click('#cameraVibrationBtn')
    _wait_for(page, "__camera.audioContexts.at(-1)?.state === 'running'")
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    assert page.evaluate('__camera.beeps') == 1
    assert not page.evaluate('__camera.vibrations.some(ms => ms > 0)')
    page.click('#cameraSoundBtn')
    _wait_for(page, "__camera.audioContexts[0].state === 'closed'")
    _decode(page, '1234567890124')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '2'")
    assert page.evaluate('__camera.beeps') == 1
    page.click('#cameraSoundBtn')
    page.click('#stopScanBtn')
    _wait_stopped(page)

    page.reload(wait_until='load')
    page.evaluate(_CAMERA)
    _start(page)
    assert page.locator('#cameraSoundBtn').get_attribute('aria-pressed') == 'true'
    assert page.locator('#cameraVibrationBtn').get_attribute('aria-pressed') == 'false'
    _wait_for(page, "__camera.audioContexts.at(-1)?.state === 'running'")
    _decode(page, '1234567890125')
    _wait_for(page, ' __camera.beeps === 1')
    assert not page.evaluate('__camera.vibrations.some(ms => ms > 0)')
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('unavailable', ['audio', 'vibration', 'both'])
def test_unsupported_feedback_is_disabled_without_breaking_scanning(camera_page, unavailable):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate("""which => {
      if (which !== 'vibration') window.AudioContext = window.webkitAudioContext = undefined;
      if (which !== 'audio') navigator.vibrate = undefined;
    }""", unavailable)
    _start(page)
    for feature, selector in (('audio', '#cameraSoundBtn'), ('vibration', '#cameraVibrationBtn')):
        button = page.locator(selector)
        label = button.locator('.when-unsupported')
        if unavailable not in (feature, 'both'):
            assert button.is_enabled()
            assert label.is_hidden()  # Being switched off is not the same as unsupported.
            assert button.get_attribute('aria-describedby') == ''
            continue
        assert button.is_disabled()
        assert button.get_attribute('aria-pressed') == 'false'
        assert 'not supported' in button.get_attribute('title')
        assert label.is_visible()
        assert label.inner_text() == 'Not supported'
        assert button.get_attribute('aria-describedby') == label.get_attribute('id')
        assert button.evaluate('el => getComputedStyle(el).opacity') == '1'
        assert button.locator('.when-off').is_hidden()
        icon = button.locator('.when-on')
        assert icon.is_visible()
        assert icon.evaluate("el => getComputedStyle(el, '::after').borderTopWidth") == '2px'
        assert icon.evaluate("el => getComputedStyle(el, '::after').transform") != 'none'
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('failure', ['constructor', 'resume'])
def test_audio_errors_do_not_break_scanning(camera_page, failure):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate("""failure => {
      if (failure === 'constructor') window.AudioContext = class { constructor() { throw new Error('Audio blocked'); } };
      else window.AudioContext.prototype.resume = () => Promise.reject(new Error('Audio blocked'));
      navigator.vibrate = () => { throw new Error('Haptics blocked'); };
    }""", failure)
    _start(page)
    page.click('#cameraSoundBtn')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Sound unavailable'")
    assert page.locator('#cameraSoundBtn').get_attribute('aria-pressed') == 'false'
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    assert page.locator('#cameraScanPulse').evaluate("el => el.classList.contains('scan-pulse')")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_feedback_toggles_work_with_site_storage_blocked(camera_page):
    page, errors = camera_page()
    page.add_init_script("""Object.defineProperty(window, 'localStorage', {
      get() { throw new Error('Site storage blocked'); }
    });""")
    page.reload(wait_until='load')
    page.evaluate(_CAMERA)
    _start(page)
    assert page.locator('#cameraSoundBtn').get_attribute('aria-pressed') == 'false'
    assert page.locator('#cameraVibrationBtn').get_attribute('aria-pressed') == 'true'
    page.click('#cameraSoundBtn')
    page.click('#cameraVibrationBtn')
    _wait_for(page, "__camera.audioContexts.at(-1)?.state === 'running'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    _start(page)
    assert page.locator('#cameraSoundBtn').get_attribute('aria-pressed') == 'true'
    assert page.locator('#cameraVibrationBtn').get_attribute('aria-pressed') == 'false'
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('capability', [None, False, [False], [True], True, [False, True], 'unavailable', 'error'])
def test_flashlight_shows_support_state_and_toggles_when_available(camera_page, capability):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate('(value) => { __camera.torchSupported = value; }', capability)
    _start(page)
    torch = page.locator('#cameraTorchBtn')
    label = page.locator('#cameraTorchBtnUnsupported')
    assert torch.is_visible()
    if capability in (True, [False, True]):
        assert torch.is_enabled()
        assert label.is_hidden()
        assert torch.get_attribute('aria-describedby') == ''
        assert torch.get_attribute('aria-pressed') == 'false'
        torch.click()
        _wait_for(page, "document.getElementById('cameraTorchBtn').getAttribute('aria-pressed') === 'true'")
        assert page.evaluate('__camera.torchOn')
        torch.click()
        _wait_for(page, "document.getElementById('cameraTorchBtn').getAttribute('aria-pressed') === 'false'")
        assert not page.evaluate('__camera.torchOn')
        assert page.evaluate('__camera.torchCalls') == [True, False]
        torch.click()
        _wait_for(page, '__camera.torchOn')
    else:
        assert torch.is_disabled()
        assert label.is_visible()
        assert label.inner_text() == 'Not supported'
        assert torch.get_attribute('aria-describedby') == label.get_attribute('id')
        assert 'not supported by this camera or browser' in torch.get_attribute('title')
        assert torch.locator('.when-on').is_visible()
        assert torch.locator('.when-off').is_hidden()
        assert torch.locator('.when-on').evaluate("el => getComputedStyle(el, '::after').transform") != 'none'
        _decode(page, '1234567890123')
        _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    page.evaluate('__camera.code = ""; __camera.torchSupported = true')
    _start(page)
    assert torch.is_enabled()
    assert label.is_hidden()
    assert torch.get_attribute('aria-describedby') == ''
    assert torch.get_attribute('title') == 'Flashlight: off'
    assert torch.get_attribute('aria-pressed') == 'false'
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('failure', ['torchFail', 'torchIgnore'])
def test_failed_flashlight_does_not_claim_it_is_on_or_stop_scanning(camera_page, failure):
    page, errors = camera_page()
    page.route('**/pending/scan', _saved)
    page.evaluate('(failure) => { __camera.torchSupported = true; __camera[failure] = true; }', failure)
    _start(page)
    page.click('#cameraTorchBtn')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Flashlight unavailable'")
    assert page.locator('#cameraTorchBtn').get_attribute('aria-pressed') == 'false'
    assert page.locator('#cameraTorchBtn').is_enabled()
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('barcode-saved-badge').textContent === '1'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_closing_during_flashlight_toggle_cleans_up_late_result(camera_page):
    page, errors = camera_page()
    page.evaluate('__camera.torchSupported = true; __camera.torchWait = true')
    _start(page)
    page.click('#cameraTorchBtn')
    _wait_for(page, "typeof __camera.releaseTorch === 'function'")
    assert page.locator('#cameraTorchBtn').is_disabled()
    page.click('#stopScanBtn')
    _wait_stopped(page)
    page.evaluate('__camera.releaseTorch()')
    page.evaluate('__camera.torchWait = false')
    _start(page)
    assert page.locator('#cameraTorchBtn').get_attribute('aria-pressed') == 'false'
    assert page.locator('#cameraTorchBtn').is_enabled()
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('failure', ['http', 'network'])
def test_failed_scan_is_visible_and_camera_keeps_running(camera_page, failure):
    page, errors = camera_page()

    def scan(route):
        if failure == 'network':
            route.abort()
        else:
            route.fulfill(status=503, json={'detail': 'Unavailable <try again>'})

    page.route('**/pending/scan', scan)
    _start(page)
    _decode(page, '1234567890123')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan needs attention'")
    text = page.locator('#cameraScanToastMessage').inner_text()
    assert ('Scan failed:' if failure == 'network' else 'Unavailable <try again>') in text
    assert page.locator('#cameraScanToastMessage try').count() == 0
    assert page.locator('#cameraScanModal').is_visible()

    page.unroute('**/pending/scan', scan)
    page.route('**/pending/scan', _saved)
    _decode(page, '1234567890124')
    _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan complete'")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('status', ['queued', 'merged'])
@pytest.mark.parametrize('failed', [False, True])
def test_lookup_toasts_identify_earlier_scans_and_do_not_replace_new_scan(camera_page, status, failed):
    page, errors = camera_page()
    codes = ['1234567890123', '1234567890124']
    items = [
        {'id': i + 1, 'barcode': code, 'name': 'Unknown', 'enriching': True}
        for i, code in enumerate(codes)
    ]
    poll_available = True

    def scan(route):
        item = next(it for it in items if it['barcode'] == route.request.post_data_json['barcode'])
        route.fulfill(json={'status': status, 'item': item})

    def pending(route):
        if poll_available:
            route.fulfill(json={'items': list(reversed(items))})
        else:
            route.fulfill(status=503, json={'detail': 'Temporarily unavailable'})

    page.route('**/pending/scan', scan)
    page.route('**/pending/', pending)
    _start(page)
    for count, code in enumerate(codes, 1):
        _decode(page, code)
        _wait_for(page, f"document.getElementById('barcode-saved-badge').textContent === '{count}'")
        assert page.locator('#cameraScanToastTitle').inner_text() == 'Barcode saved'
        assert code in page.locator('#cameraScanToastMessage').inner_text()
        assert 'looking up' in page.locator('#cameraScanToastMessage').inner_text()

    # The toast clock advances independently of the native video clock.
    _decode(page, '')
    _wait_for(page, "!document.getElementById('cameraScanPulse').classList.contains('scan-pulse')")
    # A failed poll is not a failed product lookup; keep waiting for the result.
    poll_available = False
    page.evaluate('pollEnrichingRows()')
    assert page.locator('#cameraLookupToast').is_hidden()
    poll_available = True
    page.clock.install()
    items[0].update(enriching=False, lookup_failed=failed, name='Tea <special>')
    page.evaluate('pollEnrichingRows()')
    _wait_for(page, "document.getElementById('cameraLookupToast').classList.contains('show')")
    assert page.locator('#cameraLookupToastTitle').inner_text() == (
        'Lookup failed · earlier scan' if failed else 'Product found · earlier scan')
    lookup = page.locator('#cameraLookupToastMessage').inner_text()
    assert codes[0] in lookup and codes[1] not in lookup
    assert ('saved in Pending' if failed else 'Tea <special>') in lookup
    assert page.locator('#cameraLookupToastMessage special').count() == 0
    assert codes[1] in page.locator('#cameraScanToastMessage').inner_text()

    # Another completion queues behind the first instead of silently replacing it.
    items[1].update(enriching=False, lookup_failed=False, name='Milk')
    page.evaluate('pollEnrichingRows()')
    assert codes[0] in page.locator('#cameraLookupToastMessage').inner_text()
    page.locator('#cameraLookupToast [data-bs-dismiss="toast"]').click()
    assert codes[1] in page.locator('#cameraLookupToastMessage').inner_text()
    assert 'Milk' in page.locator('#cameraLookupToastMessage').inner_text()
    page.mouse.move(0, 0)
    page.locator('#stopScanBtn').focus()  # Bootstrap pauses autohide while the toast has focus.
    page.clock.run_for(4100)
    page.evaluate('pollEnrichingRows()')
    assert page.locator('#cameraLookupToast').is_hidden()  # No duplicate completion notices.
    assert page.locator('#barcode-saved-badge').inner_text() == '2'
    assert page.evaluate('__camera.vibrations') == [150, 150]  # Lookups do not repeat recognition feedback.
    assert not page.locator('#cameraScanPulse').evaluate("el => el.classList.contains('scan-pulse')")
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_closing_camera_clears_visible_and_queued_lookup_notices(camera_page):
    page, errors = camera_page()
    _start(page)
    page.evaluate("""() => {
      queueLookupToast({barcode: '11111111', name: 'Tea', failed: false});
      queueLookupToast({barcode: '22222222', name: 'Milk', failed: false});
    }""")
    assert page.locator('#cameraLookupToast').is_visible()
    assert 'Tea' in page.locator('#cameraLookupToastMessage').inner_text()
    assert 'Milk' not in page.locator('#cameraLookupToastMessage').inner_text()
    page.click('#stopScanBtn')
    _wait_stopped(page)
    _start(page)
    assert page.locator('#cameraLookupToast').is_hidden()
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


def test_close_during_camera_permission_releases_late_stream(camera_page):
    page, errors = camera_page()
    page.evaluate('__camera.wait = true')
    page.click('#startScanBtn')
    _wait_for(page, "typeof __camera.release === 'function'")
    page.click('#stopScanBtn')
    assert page.locator('#cameraScanModal').is_hidden()
    page.evaluate('__camera.release()')
    _wait_stopped(page)
    assert page.evaluate('__camera.streams.length') == 1
    page.evaluate('__camera.wait = false')
    _start(page)
    page.click('#stopScanBtn')
    _wait_stopped(page)
    # The vendored player ignores play()'s promise; removing its video before
    # playback starts can reject that promise even though every track stopped.
    assert all('The play() request was interrupted' in error for error in errors)


def test_close_during_library_load_does_not_start_camera(camera_page):
    page, errors = camera_page()
    # Delay script insertion: the service worker can bypass Playwright's network routing.
    page.evaluate("""() => {
      const append = document.head.appendChild;
      document.head.appendChild = function(node) {
        if (!node.src?.includes('html5-qrcode.min.js')) return append.call(this, node);
        __camera.releaseLibrary = () => {
          document.head.appendChild = append;
          append.call(document.head, node);
        };
        return node;
      };
    }""")
    page.click('#startScanBtn')
    page.click('#stopScanBtn')
    page.evaluate('__camera.releaseLibrary()')
    _wait_stopped(page)
    assert page.evaluate('__camera.starts') == 0
    _start(page)
    page.click('#stopScanBtn')
    _wait_stopped(page)
    assert not errors


@pytest.mark.parametrize('stop', [
    "applyMode('consume')", "window.dispatchEvent(new PageTransitionEvent('pagehide'))",
])
def test_camera_error_allows_retry_and_leaving_stops_scanning(camera_page, stop):
    page, errors = camera_page()
    page.evaluate('__camera.fail = true')
    page.click('#startScanBtn')
    _wait_for(page, "document.getElementById('barcode-status').textContent.includes('Camera permission denied')")
    _wait_stopped(page)
    page.evaluate('__camera.fail = false')
    _start(page)
    page.evaluate(stop)
    _wait_stopped(page)
    assert not errors


def test_consume_confirmation_remains_available_after_closing_camera(camera_page):
    page, errors = camera_page()
    page.route('**/pending/scan', lambda route: route.fulfill(json={
        'status': 'consume_failed', 'barcode': '1234567890123',
        'suggestion': {'name': 'Tea'},
        'confirm': {'barcode': '1234567890123', 'product_id': 1},
    }))
    page.evaluate("selectMode('consume')")
    try:
        _start(page)
        _decode(page, '1234567890123')
        _wait_for(page, "document.getElementById('cameraScanToastTitle').textContent === 'Scan needs attention'")
        assert 'Nothing has been used up yet' in page.locator('#cameraScanToastMessage').inner_text()
        assert page.locator('#cameraScanModal').is_visible()
        page.click('#stopScanBtn')
        _wait_stopped(page)
        assert page.locator('#consume-status [data-action="consume-link"]').is_visible()
        assert not errors
    finally:
        page.evaluate("selectMode('inventory')")
