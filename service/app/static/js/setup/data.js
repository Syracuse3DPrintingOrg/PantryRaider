// Settings page data. The server renders it into the #setup-data JSON
// block in setup.html; this file, loaded plain (not deferred) right after
// it, turns it into the globals the deferred setup scripts read.

const SETUP_DATA = JSON.parse(document.getElementById('setup-data').textContent);
const TABS = SETUP_DATA.tabs;
// Pristine built-in layout for the nav editor's Reset to defaults (FoodAssistant-oret).
window.TABS_DEFAULT = SETUP_DATA.tabs_default;
// On-screen Start Page editor data (Pantry Raider): the shared custom buttons
// and the saved layout. The built-in key catalog is the deck's own (_sdCatalog),
// so the Start Page and Stream Deck editors are identical.
const START_CUSTOMS = SETUP_DATA.start_customs;
window.START_LAYOUT = SETUP_DATA.start_layout;
// Full shared custom-key definitions (same store as the Stream Deck), so the
// Start Page can render editable custom-key rows identical to the deck's.
window.START_OVERRIDES = SETUP_DATA.start_overrides;
const HAS_AUTH_PASSWORD = SETUP_DATA.has_auth_password;
const IS_CONFIGURED = SETUP_DATA.configured;
// Server-side API base for a co-hosted Grocy. Always loopback so the app
// container can reach it (mDNS may not resolve inside the container). The
// browser "Open Grocy" link is a separate, reachable URL (GROCY_BROWSER_LINK).
const PI_GROCY_DEFAULT = 'http://localhost:9383';
// Human-facing link to open Grocy in a browser (LAN IP, or <hostname>.local
// when the IP cannot be determined).
const GROCY_BROWSER_LINK = SETUP_DATA.grocy_browser_link;
const IS_SATELLITE = SETUP_DATA.is_satellite;
// Whether panes.js should ask setup/mealie/status after paint (a Pi appliance
// that is not a satellite). The probe moved out of the page render because it
// runs a docker compose ps on the device (FoodAssistant-0hwez).
const MEALIE_STATUS_PROBE = SETUP_DATA.mealie_status_probe;
// Provider-dependent model picker data (used by setup/helpers.js).
const AI_MODELS = SETUP_DATA.ai_models;
window.__APP_VERSION = SETUP_DATA.version;
// First-time wizard bootstrap values (used by setup/wizard.js).
const SETUP_CURRENT_MODE = SETUP_DATA.current_mode;
const WIZ_HAS_GROCY_KEY = SETUP_DATA.wiz_has_grocy_key;
const WIZ_HAS_AI_KEY = SETUP_DATA.wiz_has_ai_key;
const WIZ_HAS_MEALIE_KEY = SETUP_DATA.wiz_has_mealie_key;
// Whether this install is already connected to a Forager account, so the
// wizard's summary counts a Forager sign-in as AI configured.
const WIZ_CLOUD_LINKED = SETUP_DATA.wiz_cloud_linked;
