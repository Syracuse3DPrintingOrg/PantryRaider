// Initialization
// Kiosk first-time setup: reveal the on-screen wizard if the user opts out of
// finishing setup from a phone (cssj).
function showKioskWizard() {
  document.getElementById('kiosk-setup-hint')?.classList.add('d-none');
  document.getElementById('wizard-root')?.classList.remove('d-none');
}

// Deep-link compatibility (docs/design/settings-reorg.md): old #pane-* hashes
// resolve to the current pane id here, so bookmarks, docs links, and in-app
// links keep landing on the right pane when a pane is renamed. Add a row
// ('pane-old-name': 'pane-new-name') in the same change that renames a pane.
const PANE_HASH_ALIASES = {
  // Original (pre-reorg) pane ids:
  'pane-theme': 'pane-appearance',
  'pane-navigation': 'pane-appearance',
  'pane-display': 'pane-screen',
  'pane-ai': 'pane-scanning',
  'pane-hardware': 'pane-scanning',
  'pane-personalization-storage': 'pane-inventory',
  'pane-homeassistant': 'pane-connections',
  'pane-cameras': 'pane-connections',
  'pane-tunnel': 'pane-connections',
  'pane-network': 'pane-devices',
  'pane-upstream': 'pane-devices',
  'pane-data': 'pane-backups',
  // The Stream Deck editor is a sub-area of the Start Page pill; init() flips
  // the toggle to the deck when the raw hash names it.
  'pane-streamdeck': 'pane-start-page',
  // The one-menu iteration merged Mealie, the recipe sources, and the tuning
  // into a single Recipes & Meals pane; its sources half now lives in
  // Connections (the tuning is back at #pane-personalization-recipes).
  'pane-recipes': 'pane-connections',
};
function _resolvePaneHash(hash) {
  if (!hash || !hash.startsWith('#pane-')) return hash;
  const id = hash.slice(1);
  return '#' + (PANE_HASH_ALIASES[id] || id);
}

// Two side menus behind the top toggle (docs/design/settings-reorg.md,
// iteration 2): Personalization ('p', data-mgroup) holds the everyday
// taste-level panes; Settings ('s') the set-and-forget administration. One
// menu shows at a time; the last choice is remembered per device and a
// #pane-* hash, an in-page link, or a search hit selects the right menu.
let _menuGroup = 'p';
function _menuToggleButtons() {
  document.getElementById('menu-toggle-p')?.classList.toggle('active', _menuGroup === 'p');
  document.getElementById('menu-toggle-s')?.classList.toggle('active', _menuGroup === 's');
}
function _applyMenuGroup() {
  _menuToggleButtons();
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"]').forEach(function (el) {
    el.classList.toggle('d-none', (el.getAttribute('data-mgroup') || 's') !== _menuGroup);
  });
}
function _setMenuGroup(group) {
  _menuGroup = (group === 's') ? 's' : 'p';
  try { localStorage.setItem('settingsMenu', _menuGroup); } catch (e) { }
}
function showSettingsMenu(group, activate) {
  _setMenuGroup(group);
  _applyMenuGroup();
  // Keep the already-active pane when it belongs to this menu; otherwise
  // activate the requested pill, falling back to the menu's first pane.
  var pill = null, first = null;
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"]').forEach(function (el) {
    if ((el.getAttribute('data-mgroup') || 's') !== _menuGroup) return;
    if (!first) first = el;
    if (!pill && el.classList.contains('active')) pill = el;
  });
  if (activate && (activate.getAttribute('data-mgroup') || 's') === _menuGroup) pill = activate;
  pill = pill || first;
  if (pill) {
    try { bootstrap.Tab.getOrCreateInstance(pill).show(); }
    catch (e) { pill.click(); }
  }
}

// Settings search: filters the side menu to panes whose title, section
// headers, or field labels contain the query, searching both menus at once
// (hits from the other menu appear in the list and switch the toggle when
// opened), and outlines the matching section cards so the hit is findable
// inside a long pane. Clearing the box restores the chosen menu.
let _settingsSearchIndex = null;  // [{pill, panes, text}]
function _buildSettingsSearchIndex() {
  _settingsSearchIndex = [];
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"]').forEach(function (pill) {
    const target = pill.getAttribute('data-bs-target') || '';
    const pane = target ? document.querySelector(target) : null;
    if (!pane) return;
    const panes = [pane];
    // The Stream Deck editor has no pill of its own; it is reached through
    // the Start Page pill's toggle, so index its text under that pill.
    if (pill.id === 'pill-deckstart') {
      const sub = document.getElementById('pane-streamdeck');
      if (sub) panes.push(sub);
    }
    const parts = [pill.textContent];
    panes.forEach(function (p) {
      p.querySelectorAll('.pane-intro, .section-title, .block-head, label').forEach(function (el) {
        parts.push(el.textContent);
      });
    });
    _settingsSearchIndex.push({ pill: pill, panes: panes, text: parts.join(' ').toLowerCase() });
  });
}
function _clearSearchHits() {
  document.querySelectorAll('.section-card.search-hit').forEach(function (el) {
    el.classList.remove('search-hit');
  });
}
function settingsSearch(q) {
  q = (q || '').trim().toLowerCase();
  if (_settingsSearchIndex === null) _buildSettingsSearchIndex();
  _clearSearchHits();
  if (!q) {
    // Back to the chosen menu's pills; the active pane stays where it is.
    _applyMenuGroup();
    return;
  }
  var firstHit = null;
  var activeIsHit = false;
  _settingsSearchIndex.forEach(function (entry) {
    var hit = entry.text.indexOf(q) !== -1;
    entry.pill.classList.toggle('d-none', !hit);
    if (!hit) return;
    if (!firstHit) firstHit = entry;
    if (entry.pill.classList.contains('active')) activeIsHit = true;
    entry.panes.forEach(function (p) {
      p.querySelectorAll('.section-card').forEach(function (card) {
        if (card.textContent.toLowerCase().indexOf(q) !== -1) card.classList.add('search-hit');
      });
    });
  });
  // Show a matching pane so its outlined cards are visible; keep the current
  // pane when it already matches, to avoid jumping around mid-typing.
  if (firstHit && !activeIsHit) {
    try { bootstrap.Tab.getOrCreateInstance(firstHit.pill).show(); }
    catch (e) { firstHit.pill.click(); }
  }
}

// Failsafe: never leave the menu/content hidden, even if init() throws before
// it reaches the reveal step. A blank Settings page is worse than a brief flash.
function _revealMenu() {
  document.querySelectorAll('.menu-initializing').forEach(function (el) {
    el.classList.remove('menu-initializing');
  });
}
setTimeout(_revealMenu, 1500);

(function init() {
  // Keep the top toggle honest: whichever pill just showed (a click, a search
  // hit, a hash, or an in-page link) decides the visible menu. During an
  // active search only the toggle buttons move; the pill list stays filtered.
  // Attached before the hash activation below so the very first show (which
  // can fire without a transition while the menu is still hidden) is caught.
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"]').forEach(p => {
    p.addEventListener('shown.bs.tab', () => {
      _setMenuGroup(p.getAttribute('data-mgroup') || 's');
      const q = document.getElementById('settings-search');
      if (q && q.value.trim()) _menuToggleButtons(); else _applyMenuGroup();
      // The Stream Deck pane has no pill of its own (it is reached through
      // the Start Page pill's toggle), so make sure it never lingers visible
      // when any other pane opens.
      if (p.id !== 'pill-deckstart') {
        const deck = document.getElementById('pane-streamdeck');
        if (deck) deck.classList.remove('active', 'show');
      }
    });
  });
  // Opening the Start Page & Stream Deck pill programmatically (a hash or a
  // search hit skips its onclick) still initializes the right sub-area; a raw
  // #pane-streamdeck hash lands on the deck editor.
  const _dsPill = document.getElementById('pill-deckstart');
  if (_dsPill) _dsPill.addEventListener('shown.bs.tab', () =>
    showDeckStart(window.location.hash === '#pane-streamdeck' ? 'deck' : 'start'));

  // Pick the visible menu and activate the pane a #pane-* hash asks for, then
  // reveal the content, so a later failure in any optional init step cannot
  // leave the page blank. Renamed panes resolve through PANE_HASH_ALIASES
  // first; a hash pill selects its own menu, else the last-used choice wins,
  // else Personalization (the everyday menu).
  try {
    var hash0 = _resolvePaneHash(window.location.hash);
    var hashBtn0 = (hash0 && hash0.startsWith('#pane-'))
      ? document.querySelector('.side-menu [data-bs-target="' + hash0 + '"]') : null;
    var group0;
    if (hashBtn0) {
      group0 = hashBtn0.getAttribute('data-mgroup') || 's';
    } else {
      try { group0 = localStorage.getItem('settingsMenu') || 'p'; } catch (e) { group0 = 'p'; }
    }
    showSettingsMenu(group0, hashBtn0);
  } catch (e) { }
  _revealMenu();

  showProvider();
  showRecipeSource();
  scannerTypeChanged();
  renderNavEditor();
  if (!IS_SATELLITE) {
    tunnelModeChanged();
    _initTunnelStatus();
  }
  // Surface an available update as soon as Settings opens (Pi appliance or
  // server), so the card shows new software is ready without the user pressing
  // anything. Runs wherever the availability line exists; a satellite has no
  // such control.
  if (typeof checkSatelliteUpdate === 'function' && document.getElementById('update-avail')) {
    checkSatelliteUpdate(null);
  }
  // Reflect the saved debug-logging state in its toggle.
  if (typeof _loadLoggingState === 'function') _loadLoggingState();
  // Reflect this device's on-screen-events choice in its selector.
  if (typeof _haInitDeviceEvents === 'function') _haInitDeviceEvents();
  // Show current AI token usage against any budget.
  if (typeof _loadAiUsage === 'function') _loadAiUsage();
  // Forager link state and quota, when this install is linked.
  if (typeof _loadCloudStatus === 'function') _loadCloudStatus();

  // Settings form only: update auth hint on load
  if (IS_CONFIGURED) toggleAuthRequired();

  // If we arrived via a #pane-* hash, scroll it into view (the menu and pane
  // were already selected at the top of init(); a #pane-streamdeck bookmark
  // flips to the deck editor via the pill's shown handler above).
  var hash = _resolvePaneHash(window.location.hash);
  if (hash && hash.startsWith('#pane-')) {
    setTimeout(function() {
      var el = document.getElementById(hash.slice(1));
      if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'});
    }, 120);
  }
})();

document.addEventListener('DOMContentLoaded', initAiModelPickers);
document.addEventListener('DOMContentLoaded', checkApStatus);
document.addEventListener('DOMContentLoaded', loadHardwareDetect);
document.addEventListener('DOMContentLoaded', _updateGrocyOpenLink);
document.addEventListener('DOMContentLoaded', _initSyncTimes);
document.addEventListener('DOMContentLoaded', function () {
  const owns = document.getElementById('appliance_stand_mixer');
  if (owns) owns.addEventListener('change', syncStandMixerAttachments);
  syncStandMixerAttachments();
});

// Turn every info() icon into a Bootstrap tooltip so long help text collapses
// into a hover/tap popup instead of stretching a pane. Safe to call again after
// injecting new markup (it skips already-initialised icons).
function initTooltips(root) {
  if (!window.bootstrap || !bootstrap.Tooltip) return;
  (root || document).querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    if (!bootstrap.Tooltip.getInstance(el)) new bootstrap.Tooltip(el);
  });
}
document.addEventListener('DOMContentLoaded', () => initTooltips());
