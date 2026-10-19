# Camera barcode scanner component

The shared vanilla JavaScript/Bootstrap component lives in:

- `service/app/templates/_camera_barcode_scanner.html`
- `service/app/static/js/lib/camera-barcode-scanner.js`
- `service/app/static/css/camera-barcode-scanner.css`

It owns the fullscreen camera, viewport/scroll locking, duplicate suppression,
recognition pulse, sound/vibration preferences, flashlight, and two toast channels.
The host page owns saving barcodes, product lookups, and their result messages.

## Use on a page

In a template extending `base.html`, include the component once in the `scripts`
block, before the host page's deferred script:

```jinja
{% include "_camera_barcode_scanner.html" %}
<script src="static/js/pages/your-page.js?v={{ app_version }}" defer></script>
```

The include loads versioned CSS and JavaScript. The barcode decoder stays unloaded
until the first camera start or photo decode. Bootstrap and the shared page helpers
come from `base.html`.

From the host page's button-click handler:

```js
PR.cameraBarcodeScanner.start({
  title: 'Scan a barcode',
  onScan: code => handleBarcode(code),
  onError: message => PR.notice(message),
});
```

Supply both callbacks. `onScan` receives the trimmed barcode immediately; its
caller handles any asynchronous work and errors. `onError(message, level)` reports
startup failures after camera cleanup. Starting an already open scanner is ignored.
Start from a user gesture so browsers can enable sound when the user has chosen it.

## Other methods

- `stop()`: close and release camera/audio resources; also runs on Close, Escape,
  and page exit. Call it when changing what scans do.
- `showToast(html, level, title)`: update the current scan's notification.
- `queueResult(html, level, title)`: queue a separate notification for an earlier
  scan without replacing current-scan feedback.
- `loadDecoder()`: load the shared `Html5Qrcode` library for barcode-photo decoding.

Toast bodies accept trusted HTML: escape barcode/product text with `PR.esc`.
Titles are plain text. Notifications are shown only while the scanner is open,
and closing it clears queued notifications. For lookup results, include the
original barcode and an “earlier scan” title, as `manage-pantry.js` does.
