// The kiosk's own app URL, baked into the INSTALLED copy of this file by the
// provisioner (firstboot.sh) and by foodassistant-update, which replace the
// placeholder below with the device's KIOSK_URL. The repo copy keeps the
// placeholder, and content.js refuses to do anything while it is still
// present, so an unbaked install fails closed: no button, no navigation.
// Content scripts of the same extension share one isolated world, so this
// plain var is visible to content.js but never to the page itself.
var __PR_KIOSK_HOME = "__PR_KIOSK_HOME_URL__";
