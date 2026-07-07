// A discoverable "Install app" affordance (FoodAssistant-zbi4). The PWA itself
// (manifest + service worker) is set up in base.html; browsers only surface
// installation through their own subtle UI, so this adds a visible button.
//
// It appears only when installing is actually possible:
//   - Chrome/Edge/Android fire beforeinstallprompt: we capture it and the
//     button triggers the real prompt.
//   - iOS Safari never fires that event and has no programmatic install, so we
//     show the button with the manual "Share, then Add to Home Screen" steps.
// It stays hidden when the app is already installed (running standalone), when
// the user has dismissed it, and on plain-http LAN where install is impossible
// (there the Settings page explains that the secure address is needed).
(function () {
  "use strict";

  var DISMISS_KEY = "pr-install-dismissed";

  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches ||
           window.navigator.standalone === true;
  }

  function isIosSafari() {
    var ua = window.navigator.userAgent;
    var iOS = /iP(hone|ad|od)/.test(ua) ||
              // iPadOS 13+ reports as a Mac; a touch Mac is the tell.
              (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var webkit = /WebKit/.test(ua);
    var otherBrowser = /(CriOS|FxiOS|OPiOS|EdgiOS|mercury)/.test(ua);
    return iOS && webkit && !otherBrowser;
  }

  var deferredPrompt = null;

  function build(kind) {
    if (document.getElementById("pr-install-bar")) return;
    if (localStorage.getItem(DISMISS_KEY) === "1") return;

    var bar = document.createElement("div");
    bar.id = "pr-install-bar";
    bar.setAttribute("role", "dialog");
    bar.setAttribute("aria-label", "Install Pantry Raider");
    bar.style.cssText = [
      "position:fixed", "left:50%", "bottom:16px", "transform:translateX(-50%)",
      "z-index:2000", "max-width:min(92vw,420px)", "display:flex",
      "align-items:center", "gap:.6rem", "padding:.6rem .8rem",
      "background:var(--bs-secondary-bg,#2b3035)", "color:var(--bs-body-color,#fff)",
      "border:1px solid var(--bs-border-color,#444)",
      "border-radius:.7rem", "box-shadow:0 6px 24px rgba(0,0,0,.45)",
      "font-size:.95rem"
    ].join(";");

    var icon = document.createElement("span");
    icon.innerHTML = '<i class="bi bi-download"></i>';
    icon.style.cssText = "color:#F2006E;font-size:1.15rem;flex:0 0 auto";

    var text = document.createElement("div");
    text.style.cssText = "flex:1 1 auto;line-height:1.25";

    var actions = document.createElement("div");
    actions.style.cssText = "display:flex;align-items:center;gap:.4rem;flex:0 0 auto";

    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn-close btn-close-white";
    close.setAttribute("aria-label", "Dismiss");
    close.style.cssText = "flex:0 0 auto";
    close.addEventListener("click", function () {
      localStorage.setItem(DISMISS_KEY, "1");
      bar.remove();
    });

    if (kind === "prompt") {
      text.innerHTML = "Install Pantry Raider as an app for a full-screen, " +
                       "home-screen shortcut.";
      var install = document.createElement("button");
      install.type = "button";
      install.className = "btn btn-sm btn-primary";
      install.textContent = "Install";
      install.addEventListener("click", function () {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        deferredPrompt.userChoice.finally(function () {
          deferredPrompt = null;
          bar.remove();
        });
      });
      actions.appendChild(install);
    } else {
      // iOS: no programmatic install, so show the manual steps.
      text.innerHTML = "Add Pantry Raider to your Home Screen: tap the " +
                       "<strong>Share</strong> button, then " +
                       "<strong>Add to Home Screen</strong>.";
    }

    actions.appendChild(close);
    bar.appendChild(icon);
    bar.appendChild(text);
    bar.appendChild(actions);
    document.body.appendChild(bar);
  }

  if (isStandalone()) return;
  // A kiosk (the wall display) is not where anyone installs the app. Gate on
  // kiosk mode, not the URL: every app page lives under /ui/, so a path check
  // would suppress the prompt everywhere it should appear (FoodAssistant-xq5d).
  try {
    if (localStorage.getItem("kioskMode") === "true") return;
  } catch (e) { /* private mode: fall through and offer the install */ }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferredPrompt = e;
    build("prompt");
  });

  window.addEventListener("appinstalled", function () {
    var bar = document.getElementById("pr-install-bar");
    if (bar) bar.remove();
    localStorage.setItem(DISMISS_KEY, "1");
  });

  // iOS never fires beforeinstallprompt; offer the manual path directly.
  if (isIosSafari()) {
    window.addEventListener("load", function () { build("ios"); });
  }
})();
