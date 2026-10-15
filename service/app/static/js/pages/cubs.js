// Bandit Cubs page (templates/cubs.html): the broadcast warning, the browser
// flasher capability check, the copy buttons, and each board's firmware
// status. Server data arrives in the #cubs-data JSON block.

(function () {
  "use strict";
  // The status URL is absolute, with the Ingress prefix when there is one,
  // like the firmware links on the page (FoodAssistant-iaa0s).
  var CUBS_DATA = JSON.parse(document.getElementById('cubs-data').textContent);
  // Warn before flashing when the Bluetooth broadcast is off: a Cub listens
  // to it and has nothing else to show. Silent on any error.
  fetch("cub/install-tag", {credentials: "same-origin"})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.broadcast === false) {
        var el = document.getElementById("cubs-broadcast-off");
        if (el) el.classList.remove("d-none");
      }
    })
    .catch(function () {});
  // Web Serial (what <esp-web-install-button> uses) is only exposed in a secure
  // context and only by Chromium browsers. Explain WHY when it is missing, and
  // tell insecure-origin and unsupported-browser cases apart so the fix is clear.
  var secure = window.isSecureContext;
  var hasSerial = ("serial" in navigator);
  var capable = secure && hasSerial;

  var banner = document.getElementById("cubs-capability");
  if (banner && !capable) {
    if (!secure) {
      banner.className = "alert alert-warning";
      banner.innerHTML = '<i class="bi bi-shield-lock me-2"></i>' +
        '<strong>Flashing from the browser needs a secure address.</strong> ' +
        'You are on a plain http address on your network, where browsers block ' +
        'USB flashing. Open this same page over your secure (HTTPS) address, the ' +
        'one you use to reach Pantry Raider from away from home, then flash from ' +
        'there. The firmware download and command line below work from here as they are.';
    } else {
      banner.className = "alert alert-warning";
      banner.innerHTML = '<i class="bi bi-browser-chrome me-2"></i>' +
        '<strong>This browser cannot flash over USB.</strong> ' +
        'The browser flasher needs Chrome or Edge (Firefox and Safari do not ' +
        'support it). Switch to Chrome or Edge to use Install, or download the ' +
        'firmware below and flash it with the command shown.';
    }
    banner.classList.remove("d-none");
  }

  // Hide the Install button on any board when the browser cannot flash: it
  // would only report "not supported" itself. The download and command stay.
  if (!capable) {
    document.querySelectorAll(".cub-install").forEach(function (el) {
      el.classList.add("d-none");
    });
  }

  // Copy the esptool command.
  document.querySelectorAll(".cub-copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = btn.parentElement.querySelector("input");
      if (!input) return;
      var done = function () {
        var icon = btn.querySelector("i");
        if (icon) { icon.className = "bi bi-check-lg"; setTimeout(function () { icon.className = "bi bi-clipboard"; }, 1500); }
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(done).catch(function () {
          input.select(); try { document.execCommand("copy"); done(); } catch (e) {}
        });
      } else {
        input.select(); try { document.execCommand("copy"); done(); } catch (e) {}
      }
    });
  });

  // Ask the server which boards actually have an image to flash, and show the
  // right state per card (ready vs not-published-yet) instead of a button that
  // would fail. Any error degrades to "ready" so the download link is never
  // hidden by a transient hiccup.
  function applyStatus(profiles) {
    document.querySelectorAll(".cub-card").forEach(function (card) {
      var id = card.getAttribute("data-profile");
      var info = profiles && profiles[id];
      var available = !info || info.available !== false;
      var checking = card.querySelector(".cub-checking");
      var ready = card.querySelector(".cub-ready");
      var unpub = card.querySelector(".cub-unpublished");
      if (checking) checking.classList.add("d-none");
      if (available) {
        if (ready) ready.classList.remove("d-none");
        if (unpub) unpub.classList.add("d-none");
      } else {
        if (ready) ready.classList.add("d-none");
        if (unpub) unpub.classList.remove("d-none");
      }
    });
  }
  fetch(CUBS_DATA.status_url, { headers: { "Accept": "application/json" } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { applyStatus(d && d.profiles); })
    .catch(function () { applyStatus(null); });
})();
