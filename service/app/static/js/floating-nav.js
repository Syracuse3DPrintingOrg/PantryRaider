// On-screen navigation bar (FoodAssistant-bzuu, -i181).
//
// A FIXED bar docked to a screen edge (bottom, left, or right), not a draggable
// floating overlay. The edge is the server default (data-position) unless the
// device has its own choice in localStorage (placement is per-device: a wall
// kiosk and a phone want different docking). The bar reserves layout space by
// padding the body on the docked side, so it never sits on top of content.
//
// On a touch kiosk it auto-docks to the bottom when nothing is set, so the
// large icon targets are always available without the hamburger.
//
// Auto-hides when a Stream Deck is connected if that option is set, since the
// deck already provides navigation.
(function () {
  var EDGES = ['bottom', 'left', 'right'];
  var STORE_KEY = 'floatNavPosition';

  // Map a legacy corner value (from the older draggable menu) to an edge so
  // existing saved settings keep working.
  function normalizeEdge(value) {
    if (EDGES.indexOf(value) !== -1) return value;
    if (value === 'top-left' || value === 'bottom-left') return 'left';
    if (value === 'top-right' || value === 'bottom-right') return 'right';
    return '';  // 'off', '', or unknown
  }

  function start() {
    var nav = document.getElementById('floatNav');
    if (!nav) return;

    var serverPos = nav.getAttribute('data-position') || 'off';
    var autohide = nav.getAttribute('data-autohide-streamdeck') === '1';
    var hasDeck = nav.getAttribute('data-has-streamdeck') === '1';

    // Server-resolved "hide the on-screen nav entirely" (nav_visibility): on a
    // Stream-Deck kiosk at large scale the deck is the navigation surface, so
    // the floating bar just eats the small panel. Keep it off, even in kiosk
    // mode, and leave the top navbar's hamburger as the on-screen escape
    // (data-floatnav-active stays unset, so that navbar keeps its toggler).
    if (nav.getAttribute('data-nav-hidden') === '1') {
      nav.classList.add('d-none');
      clearPadding();
      document.documentElement.removeAttribute('data-floatnav-active');
      return;
    }

    // Per-device override beats the server default.
    var stored = '';
    try { stored = localStorage.getItem(STORE_KEY) || ''; } catch (e) { }
    var raw = stored || serverPos;
    var edge = normalizeEdge(raw);

    // On a touch kiosk, dock to the bottom whenever no edge is in effect, so
    // navigation is always one tap away. Only an explicit per-device 'off'
    // suppresses it: the server default is 'off' for ordinary browsers, but a
    // kiosk still needs on-screen navigation, so that default does not block it.
    var kiosk = false;
    try { kiosk = localStorage.getItem('kioskMode') === 'true'; } catch (e) { }
    if (kiosk && !edge && stored !== 'off') {
      edge = 'bottom';
    }

    if (!edge || (autohide && hasDeck)) {
      nav.classList.add('d-none');
      clearPadding();
      // Signal CSS that the floating nav is NOT the active nav surface, so the
      // top navbar keeps its full primary-tab row even at high zoom (otherwise
      // a high-scale small panel would have no visible tab navigation at all).
      document.documentElement.removeAttribute('data-floatnav-active');
      return;
    }

    applyDock(nav, edge);
    nav.classList.remove('d-none');
    reserveSpace(nav, edge);
    // The floating nav is the single on-screen nav surface here. Publish that as
    // an attribute so base.html can slim the redundant top navbar at high zoom
    // on a small panel (FoodAssistant-fuuj) and reclaim the screen for content.
    document.documentElement.setAttribute('data-floatnav-active', '1');

    window.addEventListener('resize', function () {
      reserveSpace(nav, edge);
    });
  }

  function applyDock(nav, edge) {
    EDGES.forEach(function (e) { nav.classList.remove('float-nav-dock-' + e); });
    nav.classList.add('float-nav-dock-' + edge);
  }

  // Pad the body on the docked side so the fixed bar never overlaps content.
  // The body is the right target (its padding-top already offsets the navbar);
  // we only ever touch left/right/bottom here, never top.
  function reserveSpace(nav, edge) {
    clearPadding();
    var rect = nav.getBoundingClientRect();
    var body = document.body;
    var root = document.documentElement;
    // Publish the dock size as a CSS variable too, so other fixed widgets (the
    // floating timer window) can offset themselves and not sit under the bar.
    if (edge === 'bottom') {
      body.style.setProperty('padding-bottom', Math.ceil(rect.height) + 'px', 'important');
      root.style.setProperty('--float-nav-bottom', Math.ceil(rect.height) + 'px');
    } else if (edge === 'left') {
      body.style.setProperty('padding-left', Math.ceil(rect.width) + 'px', 'important');
      root.style.setProperty('--float-nav-left', Math.ceil(rect.width) + 'px');
    } else if (edge === 'right') {
      body.style.setProperty('padding-right', Math.ceil(rect.width) + 'px', 'important');
      root.style.setProperty('--float-nav-right', Math.ceil(rect.width) + 'px');
    }
  }

  function clearPadding() {
    var body = document.body;
    ['padding-bottom', 'padding-left', 'padding-right'].forEach(function (p) {
      body.style.removeProperty(p);
    });
    var root = document.documentElement;
    ['--float-nav-bottom', '--float-nav-left', '--float-nav-right'].forEach(function (v) {
      root.style.removeProperty(v);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
