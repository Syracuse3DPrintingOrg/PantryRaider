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
  // Home Assistant now has its own pane (FoodAssistant-s6q); the old anchor
  // lands there. Cameras stay in the (relabelled) Connections pane.
  'pane-homeassistant': 'pane-home-assistant',
  'pane-cameras': 'pane-connections',
  // Forager sign-in and remote access now live in their own pane, so the old
  // tunnel and cloud anchors land there (on a main install; a satellite has no
  // Forager pane and falls back to the default menu).
  'pane-tunnel': 'pane-forager',
  'pane-cloud': 'pane-forager',
  // Wi-Fi and the two hostname fields moved to the dedicated Network pane
  // (FoodAssistant-42n4); the satellite upstream cards stay in the Fleet pane.
  'pane-upstream': 'pane-devices',
  'pane-data': 'pane-backups',
  // The Stream Deck editor is a sub-area of the Start Page pill; init() flips
  // the toggle to the deck when the raw hash names it.
  'pane-streamdeck': 'pane-start-page',
  // Mealie and the recipe sources moved to the Recipes pane under Kitchen, so
  // the old Recipes anchor lands there (the taste tuning already lived there).
  'pane-recipes': 'pane-personalization-recipes',
};
function _resolvePaneHash(hash) {
  if (!hash || !hash.startsWith('#pane-')) return hash;
  const id = hash.slice(1);
  return '#' + (PANE_HASH_ALIASES[id] || id);
}

// One always-visible settings menu, grouped under four plain-language
// headings (Kitchen, This Device, Connections, System). The old two-menu
// Personalization/Settings toggle is gone: everything is one click away, so
// nothing (Wi-Fi in particular) is buried behind a toggle. This helper just
// restores every pill and heading after a search filters them.
function _showAllMenuItems() {
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"], .side-menu .menu-heading')
    .forEach(function (el) { el.classList.remove('d-none'); });
}

// Open a settings pane by id from the Overview landing cards (FoodAssistant-jcnh).
// Clicking the real side-menu pill (rather than showing the pane directly) keeps
// the sidebar highlight in sync and runs that pill's own onclick loaders.
function openSettingsPane(paneId) {
  const pill = document.querySelector('.side-menu [data-bs-target="#' + paneId + '"]');
  if (pill) {
    try { bootstrap.Tab.getOrCreateInstance(pill).show(); }
    catch (e) { pill.click(); return; }
    // getOrCreateInstance().show() does not fire the pill's inline onclick, so
    // run it too for panes that lazy-load status on open.
    if (typeof pill.onclick === 'function') pill.onclick();
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Settings search: filters the side menu to panes whose title, section
// headers, or field labels contain the query, and outlines the matching
// section cards so the hit is findable inside a long pane. The group headings
// are hidden while a search is active (their pills are filtered individually).
// Clearing the box restores the full grouped menu.
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
    // Back to the full grouped menu; the active pane stays where it is.
    _showAllMenuItems();
    return;
  }
  // Hide the group headings while filtering; each pill is shown or hidden on
  // its own match below, so a heading over an empty group would just be noise.
  document.querySelectorAll('.side-menu .menu-heading')
    .forEach(function (el) { el.classList.add('d-none'); });
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
  // Keep the Stream Deck sub-pane from lingering: it has no pill of its own
  // (it is reached through the Start Page pill's toggle), so clear it whenever
  // any other pane opens. Attached before the hash activation below so the very
  // first show (which can fire without a transition while the menu is still
  // hidden) is caught.
  document.querySelectorAll('.side-menu [data-bs-toggle="pill"]').forEach(p => {
    p.addEventListener('shown.bs.tab', () => {
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

  // Activate the pane a #pane-* hash asks for, then reveal the content, so a
  // later failure in any optional init step cannot leave the page blank.
  // Renamed panes resolve through PANE_HASH_ALIASES first; with one grouped
  // menu there is no menu to switch, so the default active pane (Overview, the
  // landing) stands unless a hash names another.
  try {
    var hash0 = _resolvePaneHash(window.location.hash);
    var hashBtn0 = (hash0 && hash0.startsWith('#pane-'))
      ? document.querySelector('.side-menu [data-bs-target="' + hash0 + '"]') : null;
    if (hashBtn0) {
      try { bootstrap.Tab.getOrCreateInstance(hashBtn0).show(); }
      catch (e) { hashBtn0.click(); }
    }
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
  // Reflect this device's persistent-submenu choice in its selector.
  if (typeof _navSubmenusInit === 'function') _navSubmenusInit();
  // Show current AI token usage against any budget.
  if (typeof _loadAiUsage === 'function') _loadAiUsage();
  // Forager link state and quota, when this install is linked.
  if (typeof _loadCloudStatus === 'function') _loadCloudStatus();
  // Forager remote-access card, when linked on a Pi appliance.
  if (typeof _loadTunnelStatus === 'function') _loadTunnelStatus();
  // Forager sign-in extras: reveal "Continue with Google" when the cloud
  // offers it, and surface a ?cloud_error= message from a sign-in bounce.
  if (typeof _initCloudMeta === 'function') _initCloudMeta();
  if (typeof _showCloudReturnNotice === 'function') _showCloudReturnNotice();

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
