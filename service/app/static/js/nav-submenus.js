// Persistent submenus (FoodAssistant-ohro).
//
// Normally the top navbar tucks a section's sub-items into a collapsed dropdown;
// you click the section to see them. With this on, the section you are on stays
// expanded so all its sub-items are in view the whole time (Kitchen Guide shows
// Convert, Nutrition, Camera while you are on any Kitchen Guide page). base.html
// marks the active section with .nav-section-active and styles the inline layout
// off the html[data-persistent-submenus="1"] flag this script sets.
//
// The default follows the device: on for an ordinary browser and for a small
// panel (data-default from the server), off for a larger kiosk where collapsed
// menus keep the big buttons tidy. A per-device choice in Settings overrides it
// and is remembered in this browser only (localStorage 'navPersistentSubmenus':
// 'on', 'off', or absent to follow the default).
(function () {
  var STORE_KEY = 'navPersistentSubmenus';

  function start() {
    var cfg = document.getElementById('nav-submenus-config');
    var serverDefault = cfg && cfg.getAttribute('data-default') === '1';

    var override = null;
    try { override = localStorage.getItem(STORE_KEY); } catch (e) { }

    var enabled;
    if (override === 'on') {
      enabled = true;
    } else if (override === 'off') {
      enabled = false;
    } else {
      // No per-device choice: an ordinary browser (not kiosk mode) is "the web"
      // and defaults on; a kiosk follows the server-resolved default, which is on
      // only at the small panel scale.
      var kiosk = false;
      try { kiosk = localStorage.getItem('kioskMode') === 'true'; } catch (e) { }
      enabled = kiosk ? serverDefault : true;
    }

    var root = document.documentElement;
    if (!enabled) {
      root.removeAttribute('data-persistent-submenus');
      return;
    }
    root.setAttribute('data-persistent-submenus', '1');

    // The active section now shows its sub-items inline, so its toggle should not
    // also pop the dropdown open on top. Drop the Bootstrap toggle wiring and
    // mark it expanded; the section's own page is still reachable as the first
    // inline item. Nothing else about the menu contents changes.
    var toggle = document.querySelector(
      '.nav-primary .nav-item.dropdown.nav-section-active > .dropdown-toggle');
    if (toggle) {
      toggle.removeAttribute('data-bs-toggle');
      toggle.setAttribute('aria-expanded', 'true');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
