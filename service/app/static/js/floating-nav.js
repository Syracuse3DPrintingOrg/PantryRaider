// On-screen floating navigation menu (FoodAssistant-bzuu, FoodAssistant-76mw).
//
// Places a compact set of nav icons in a screen corner. The corner is the
// server default (data-position) unless the user has dragged it on this device,
// in which case a localStorage override wins (position is inherently per-device:
// a wall kiosk and a phone want different placement). Dragging the handle moves
// the menu; on release it snaps to the nearest corner and the choice is saved.
//
// Orientation (vertical column vs horizontal row) is also per-device: the server
// default (data-orientation) is the baseline, and a per-device localStorage
// override beats it so a tall phone and a wide display can differ.
//
// When the menu is shown it pads the page content on the docked edge so the
// floating icons never sit on top of the page text.
//
// Auto-hides when a Stream Deck is connected if that option is set, since the
// deck already provides navigation.
(function () {
  var CORNERS = ['top-left', 'top-right', 'bottom-left', 'bottom-right'];
  var ORIENTATIONS = ['vertical', 'horizontal'];
  var STORE_KEY = 'floatNavPosition';
  var ORIENT_KEY = 'floatNavOrientation';
  var GAP_PX = 12;  // breathing room between the menu and the content it pads

  function start() {
    var nav = document.getElementById('floatNav');
    if (!nav) return;

    var serverPos = nav.getAttribute('data-position') || 'off';
    var serverOrient = nav.getAttribute('data-orientation') || 'vertical';
    var autohide = nav.getAttribute('data-autohide-streamdeck') === '1';
    var hasDeck = nav.getAttribute('data-has-streamdeck') === '1';

    // Per-device overrides beat the server defaults.
    var stored = '';
    try { stored = localStorage.getItem(STORE_KEY) || ''; } catch (e) { }
    var pos = CORNERS.indexOf(stored) !== -1 || stored === 'off' ? stored : serverPos;

    var storedOrient = '';
    try { storedOrient = localStorage.getItem(ORIENT_KEY) || ''; } catch (e) { }
    var orient = ORIENTATIONS.indexOf(storedOrient) !== -1 ? storedOrient : serverOrient;

    // On a touch kiosk the floating nav is the primary way to get around (the
    // top tabs collapse behind the hamburger), so default it on when nothing is
    // set. An explicit stored 'off' on this device still wins. Default to a
    // horizontal row along the bottom: a tall column of tabs would run most of
    // the height of a short panel.
    var kiosk = false;
    try { kiosk = localStorage.getItem('kioskMode') === 'true'; } catch (e) { }
    if (kiosk && pos === 'off' && stored !== 'off') {
      pos = 'bottom-right';
      if (ORIENTATIONS.indexOf(storedOrient) === -1) orient = 'horizontal';
    }

    if (pos === 'off' || (autohide && hasDeck)) {
      nav.classList.add('d-none');
      clearPadding();
      return;
    }
    applyOrientation(nav, orient);
    var corner = CORNERS.indexOf(pos) !== -1 ? pos : 'top-right';
    applyCorner(nav, corner);
    nav.classList.remove('d-none');

    padContent(nav, corner, orient);
    wireDrag(nav);

    // The menu shape changes the padding; recompute on resize.
    window.addEventListener('resize', function () {
      padContent(nav, currentCorner(nav) || corner, currentOrientation(nav));
    });
  }

  function applyOrientation(nav, orient) {
    if (orient === 'horizontal') {
      nav.classList.add('float-nav-horizontal');
    } else {
      nav.classList.remove('float-nav-horizontal');
    }
  }

  function currentOrientation(nav) {
    return nav.classList.contains('float-nav-horizontal') ? 'horizontal' : 'vertical';
  }

  function applyCorner(nav, corner) {
    for (var i = 0; i < CORNERS.length; i++) {
      nav.classList.remove('float-nav-pos-' + CORNERS[i]);
    }
    nav.classList.add('float-nav-pos-' + corner);
    // Clear any inline offsets left over from a drag.
    nav.style.top = nav.style.left = nav.style.right = nav.style.bottom = '';
  }

  function currentCorner(nav) {
    for (var i = 0; i < CORNERS.length; i++) {
      if (nav.classList.contains('float-nav-pos-' + CORNERS[i])) return CORNERS[i];
    }
    return null;
  }

  // Pad the page content so the docked menu does not overlap it. A vertical
  // column hugs a left/right edge, so we pad that side by the menu width; a
  // horizontal row hugs a top/bottom edge, so we pad that side by its height.
  function padContent(nav, corner, orient) {
    var content = document.getElementById('pageContent');
    if (!content) return;
    clearPadding();
    var rect = nav.getBoundingClientRect();
    // Set the reservation with !important so it beats kiosk.css's gutter rules
    // (which are !important and would otherwise let the menu overlap content,
    // most visibly in vertical mode where it pads the same left/right side).
    if (orient === 'horizontal') {
      var v = (corner.indexOf('top') === 0) ? 'padding-top' : 'padding-bottom';
      content.style.setProperty(v, Math.ceil(rect.height + GAP_PX) + 'px', 'important');
    } else {
      var h = (corner.indexOf('left') !== -1) ? 'padding-left' : 'padding-right';
      content.style.setProperty(h, Math.ceil(rect.width + GAP_PX) + 'px', 'important');
    }
  }

  function clearPadding() {
    var content = document.getElementById('pageContent');
    if (!content) return;
    ['padding-top', 'padding-bottom', 'padding-left', 'padding-right'].forEach(function (p) {
      content.style.removeProperty(p);
    });
  }

  function nearestCorner(cx, cy) {
    var vert = cy < window.innerHeight / 2 ? 'top' : 'bottom';
    var horiz = cx < window.innerWidth / 2 ? 'left' : 'right';
    return vert + '-' + horiz;
  }

  function wireDrag(nav) {
    var handle = nav.querySelector('.float-nav-handle');
    if (!handle) return;
    var dragging = false, offX = 0, offY = 0;

    handle.addEventListener('pointerdown', function (e) {
      dragging = true;
      var rect = nav.getBoundingClientRect();
      offX = e.clientX - rect.left;
      offY = e.clientY - rect.top;
      nav.classList.add('dragging');
      for (var i = 0; i < CORNERS.length; i++) {
        nav.classList.remove('float-nav-pos-' + CORNERS[i]);
      }
      nav.style.right = nav.style.bottom = '';
      nav.style.left = rect.left + 'px';
      nav.style.top = rect.top + 'px';
      handle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    handle.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      nav.style.left = (e.clientX - offX) + 'px';
      nav.style.top = (e.clientY - offY) + 'px';
    });

    function end(e) {
      if (!dragging) return;
      dragging = false;
      nav.classList.remove('dragging');
      var rect = nav.getBoundingClientRect();
      var corner = nearestCorner(rect.left + rect.width / 2, rect.top + rect.height / 2);
      applyCorner(nav, corner);
      try { localStorage.setItem(STORE_KEY, corner); } catch (err) { }
      padContent(nav, corner, currentOrientation(nav));
    }
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
