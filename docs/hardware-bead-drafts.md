# Hardware bead fix drafts (Group D)

Proposed fixes for the Pi/display/Stream Deck bugs that need a physical device
to verify. These are grounded in the current code but NOT yet applied, because a
wrong guess in the boot/compositor path can leave a kiosk unbootable. Apply one
at a time on a test device, confirm, then we land it.

Each entry: symptom, most likely cause, the concrete change to try, and how to
confirm on-device.

## 1dp5 - Mouse cursor still shows with no pointer

- Cause: the blank-Xcursor theme (`_install_blank_cursor_theme` in
  `firstboot.sh`) only works if cage honours `XCURSOR_THEME`/`XCURSOR_PATH`.
  Newer cage/wlroots draw their own pointer and ignore the Xcursor theme, so the
  cursor returns.
- Try: hide the pointer at the compositor instead of via Xcursor.
  - Preferred: run the kiosk session under `seatd`/`cage` with `wlopm`-free
    pointer hiding by launching chromium with a 1x1 transparent system cursor is
    not enough on Wayland; instead install `interception-tools` or, simplest,
    add a tiny `unclutter-xfixes` equivalent for Wayland: `wlrctl pointer` has no
    hide. The reliable lever on cage is the env `WLR_NO_HARDWARE_CURSORS=1` plus
    a 0-size cursor theme; if that still shows, switch the launch to
    `cage -- chromium ... --hide-cursor` is unsupported, so fall back to a
    udev rule that, when no `ID_INPUT_MOUSE` device is present, sets the cursor
    size to 0 via `XCURSOR_SIZE=0` in the kiosk unit's `Environment=`.
  - Quick test lever first: add `Environment=XCURSOR_SIZE=1` and
    `WLR_NO_HARDWARE_CURSORS=1` to `foodassistant-kiosk.service`, reboot, confirm.
- Confirm: boot headless (no USB mouse), watch the panel for a stray pointer;
  plug a mouse, confirm it returns (for `auto`).

## 9ext - Kiosk freeze after calibration + cloud-init-only on reboot

This is the highest-priority hardware bug (P1) and has three linked symptoms.

1. Freeze after calibration (a TTY/"code window" instead of the kiosk).
   - Cause: the host bridge restarts `foodassistant-kiosk.service` (or runs
     `foodassistant-apply-rotation`) while the calibration page is still being
     served, and `cage` is torn down before its Wayland socket is ready, so cage
     comes up half-dead.
   - Try: in the bridge's calibration-apply path, do NOT restart the kiosk
     synchronously from within the request. Instead write the new matrix, then
     schedule the restart with a short `systemd-run --on-active=2s` so the HTTP
     response returns first and cage is restarted cleanly once. Guard
     `foodassistant-apply-rotation` with a wait-for-socket loop
     (`until [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; do sleep 0.2; done` capped at
     ~10s) before it tries to talk to the compositor.
2. Cloud-init target on reboot, kiosk never starts.
   - Cause: `foodassistant-kiosk.service` ordering. If it is `After=cloud-init`
     but the graphical/seat target is not yet up, it starts and exits.
   - Try: set `After=systemd-user-sessions.service seatd.service` and
     `Wants=seatd.service`, `Restart=on-failure`, `RestartSec=3`, and a
     `StartLimitIntervalSec=0` so a transient early-boot failure self-heals
     instead of parking at cloud-init. Capture `systemctl status
     foodassistant-kiosk` + `journalctl -u foodassistant-kiosk -b` to confirm the
     exit reason first.
3. First-dot calibration failure after a rotation recovery.
   - Cause: the evtest SSE stream / input-group permission is re-opened before
     the device node settles after the rotation re-init, so the first tap is
     dropped.
   - Try: in the calibration page, discard touches for the first ~300ms after
     the SSE stream opens, and re-open the evdev device on first-read EAGAIN.

## 9ohp - 7-inch touch not rotated with the display

- Cause: rotating the video (KMS/compositor) does not rotate the touch
  coordinates; libinput needs a calibration matrix matching the rotation.
- Try: this is what the `_matrix_for_rotation` / `_ROT_AFFINE` work in the host
  bridge already computes. Confirm the libinput udev rule
  (`LIBINPUT_CALIBRATION_MATRIX`) is actually re-applied on a rotation change for
  the DSI touch device, not only on first calibration. Add a
  `foodassistant-apply-rotation` step that re-writes the calibration udev rule
  and `udevadm trigger`s the touch device after a rotation change.
- Confirm: rotate via the web UI, tap the four corners, confirm hits land.

## mox4 - DSI 7-inch touch calibration does not apply

- Note already on the bead: the calibration page 500 was fixed in 81fcb19;
  re-test first. If taps still do not apply: the DSI panel's touch device may not
  be the one the calibration matrix is written for (wrong `ID_INPUT` match).
- Try: log the matched evdev device name in the calibration flow and confirm the
  udev `ENV{LIBINPUT_CALIBRATION_MATRIX}` rule's `KERNELS==`/`ATTRS{name}==`
  matches the DSI touch controller exactly.

## n52 - Waveshare HDMI HAT touchscreen not registering

- Depends on 8ji (ADS7846 calibration) which is closed. The HDMI HAT touch is a
  USB HID or ADS7846 SPI device; in the cage/chromium session it needs to be in
  the input seat.
- Try: confirm the touch device shows in `libinput list-devices` inside the
  kiosk session; if absent, add it to the seat via a `udev`
  `ENV{ID_SEAT}="seat0"` / `TAG+="seat"` rule, then restart cage.

## kyl2 - Display blank until a reboot after first setup

- Cause: the kiosk/seat/compositor stack is not (re)started at the end of
  first-time setup, so the panel stays on the console until a reboot brings the
  units up in the right order.
- Try: at the end of the setup wizard's "apply" on a Pi, have the host bridge
  run `systemctl restart foodassistant-kiosk` (and `seatd` if needed) once, with
  the same deferred `systemd-run --on-active` pattern as 9ext so it does not race
  the in-flight request. This is the "trigger a clean restart automatically"
  fallback the bead asks for.

## 3mq - Sluggish buttons/timers on Pi 3B+

- Cause: per-press latency from synchronous-feeling HTTP in the controller, plus
  possible undervoltage on the 3B+.
- Try (software): in `controller.py`, ensure the key handler returns immediately
  and the HTTP side effect is fully fire-and-forget (it already schedules via
  `run_coroutine_threadsafe`); audit `_handle` for any awaited network call that
  blocks the redraw. Cache the status poll so a press never waits on a poll.
  Shorten `httpx` connect timeouts. Rule out undervoltage with
  `vcgencmd get_throttled` (bead notes it reported undervoltage).
- Confirm: measure press-to-redraw on the 3B+ before/after.

## k9a8 - Satellite breaks when the host IP changes (BUILDABLE in software)

- This one is not really hardware: it is the best next software build.
- Plan: the server already knows its hostname (`device_hostname`). Include it in
  the satellite config payload, and on the satellite store an mDNS fallback
  (`<host>.local`). When `sync_from_upstream` hits a ConnectError on the stored
  IP, re-resolve `<host>.local` (and/or re-run the LAN scan for a FoodAssistant
  instance) and update `remote_server_ip`. Prefer the `.local` name in the
  upstream URL when the host advertises mDNS.
- Confirm: change the host's DHCP lease, verify the satellite reconnects without
  manual edits.

## zgae - Stream Deck + / Neo support

- Needs the actual hardware. The Plus has 8 keys + a touch strip + 4 dials; the
  Neo has 8 keys + 2 touch points + an info bar. The `python-elgato-streamdeck`
  lib exposes these; `layout.py`/`controller.py` assume a key grid only.
- Plan: add the device key counts to `SUPPORTED_SIZES`, ignore the dials/touch
  strip initially (render only the key grid), then add dial/touch handlers in a
  follow-up. Verify on the device.
