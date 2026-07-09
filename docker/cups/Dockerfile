# Pantry Raider CUPS sidecar (FoodAssistant-32dd).
#
# The server-mode app (with-printing compose profile) talks to this CUPS server
# to discover, add, and print to network label and document printers. It builds
# on olbat/cupsd (CUPS + cups-filters, including the rastertolabel filter Zebra
# ZPL printers need) and bakes in:
#   * our cupsd.conf, tuned so the app can manage queues over the compose
#     network with no manual setup (ServerAlias, LAN admin without a password),
#   * the Zebra ZPL PPD at a stable path the app's Add-a-printer flow references
#     (services/printing.py PPD_DRIVERS).
# Pinned by digest for reproducible builds (matches the compose reference).
FROM olbat/cupsd@sha256:28c47cfe20d59c94e07c37c3759d469624ba209daa0e5821ed3b6b1d6f665bf5

COPY cupsd.conf /etc/cups/cupsd.conf
COPY zebra-zpl.ppd /usr/share/ppd/foodassistant/zebra-zpl.ppd
