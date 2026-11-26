/*
 * The wall tablet profile (static/css/wall.css): the few things a stylesheet
 * cannot do on its own.
 *
 * On Glance (templates/start.html):
 *   - More shows the pages that are not among the four big tiles, below it,
 *     and Fewer puts them away again.
 *   - "Continue cooking" names the recipe open on this screen and the step
 *     it is on, from GET api/screens/recipe?screen=<id>. A server without
 *     that answer (404) is asked GET current-recipe instead, and the card
 *     names the recipe without a step. It is asked again when the page comes
 *     back into view, never on a timer.
 * On every page with the menu:
 *   - A tap outside the open menu closes it, and does nothing else.
 *
 * Plain ES5 with XMLHttpRequest, like the other tablet scripts.
 */
(function () {
  'use strict';
  var html = document.documentElement;
  if (html.getAttribute('data-profile') !== 'wall') return;
  var SCREEN = html.getAttribute('data-screen-id') || '';
  // Where the card goes when the answer names no page of its own.
  var COOK_PATH = 'ui/current-recipe?cook=1';

  function getJSON(url, done) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.timeout = 10000;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      done(xhr.status, data);
    };
    try { xhr.send(null); } catch (e) { done(0, null); }
  }

  // --- Continue cooking ------------------------------------------------------
  var card = document.getElementById('wall-continue');
  var titleEl = document.getElementById('wall-continue-title');
  var stepEl = document.getElementById('wall-continue-step');
  var asking = false;

  // Only a page of this app, so the card never takes the tablet somewhere
  // it has no way back from.
  function appPath(p) {
    p = typeof p === 'string' ? p.replace(/^\/+/, '') : '';
    return /^ui\/[A-Za-z0-9_\-\/]*(\?[A-Za-z0-9_\-=&]*)?$/.test(p) ? p : '';
  }

  // A long recipe name is the part that shortens; the step always shows.
  function show(title, step, path) {
    title = String(title || '').replace(/\s+/g, ' ').replace(/^\s+|\s+$/g, '');
    if (!title) { card.hidden = true; return; }
    titleEl.textContent = title;
    stepEl.textContent = step ? ', step ' + step : '';
    card.setAttribute('href', appPath(path) || COOK_PATH);
    card.hidden = false;
  }

  function fromCurrentRecipe() {
    getJSON('current-recipe', function (status, d) {
      asking = false;
      if (status !== 200) return;          // offline for a moment: keep what shows
      var r = d && d.recipe;
      if (r && r.title) show(r.title, null, '');
      else card.hidden = true;             // nothing is cooking
    });
  }

  function loadRecipe() {
    if (!card || !titleEl || !stepEl || asking) return;
    asking = true;
    getJSON('api/screens/recipe?screen=' + encodeURIComponent(SCREEN), function (status, d) {
      if (status === 200 && d) {
        asking = false;
        if (d.ok === false) { card.hidden = true; return; }
        var title = (d.recipe && d.recipe.title) || d.title;
        var step = typeof d.step === 'number' && d.step > 0 ? d.step : null;
        show(title, step, d.path || '');
        return;
      }
      if (status === 404 || status === 405) { fromCurrentRecipe(); return; }
      asking = false;
    });
  }

  if (card) {
    loadRecipe();
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) loadRecipe();
    });
    window.addEventListener('pageshow', function (ev) {
      if (ev.persisted) loadRecipe();
    });
  }

  // --- More ------------------------------------------------------------------
  var more = document.getElementById('wall-more');
  var rest = document.getElementById('wall-rest');
  if (more && rest) {
    var label = more.querySelector('.wall-more-label');
    more.addEventListener('click', function () {
      var open = more.getAttribute('aria-expanded') !== 'true';
      more.setAttribute('aria-expanded', open ? 'true' : 'false');
      rest.hidden = !open;
      if (label) label.textContent = label.getAttribute(open ? 'data-label-fewer' : 'data-label-more') || '';
      if (open) {
        try { rest.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { rest.scrollIntoView(false); }
      } else {
        var wrap = more.parentNode;
        if (wrap) wrap.scrollTop = 0;
      }
    });
  }

  // --- The menu --------------------------------------------------------------
  var menu = document.getElementById('nav');
  if (menu) {
    document.addEventListener('click', function (ev) {
      if (!menu.classList.contains('show')) return;
      var bar = menu.parentNode;
      while (bar && !(bar.tagName === 'NAV' && /\bnavbar\b/.test(bar.className))) bar = bar.parentNode;
      if (bar && bar.contains(ev.target)) return;
      // A tap beside an open menu only closes it; whatever is under the
      // finger is left alone.
      ev.preventDefault();
      ev.stopPropagation();
      var B = window.bootstrap;
      if (B && B.Collapse) B.Collapse.getOrCreateInstance(menu).hide();
      else menu.classList.remove('show');
    }, true);
  }
})();
