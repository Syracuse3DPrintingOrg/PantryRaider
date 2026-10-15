# Printing labels and documents

Pantry Raider can print food labels for your pantry and send a recipe to a
regular printer when you would rather cook from paper. Printing is off until
you turn it on, and nothing about your setup changes until you do.

For the small Bluetooth thermal label printers and the exact steps to pair
one, see [Label printing hardware](hardware/label-printing.md). This page
covers what you can print, adding a printer, sharing printers across your
devices, and designing your own label.

## What you can print

- **Food labels.** Print a label for any item straight from your inventory,
  or a whole batch at once when you bring in new stock. A food label carries
  the name, the date you added it, and the best-by date, with a small chip
  next to the best-by that says how that date was set: you typed it, it came
  from the built-in shelf-life rules (shown as "est."), or the AI worked it
  out (shown as "AI"). A date you edit yourself prints with no caveat.
  A label printed for something in your inventory also carries a small square
  **scan-to-use-up code** identifying the exact container it is stuck on:
  when that container is finished, scan the label and that entry comes off
  your stock, the right one even when three of the same thing sit in the
  fridge with different dates. It is the same code Grocy's own scanners
  understand, and it follows whichever scanning mode is active (during a
  stock count the scan just counts the item, nothing is removed). Labels for
  things that are not in your inventory, and labels too small for a readable
  code, print exactly as before.
- **Spice and decorative labels.** A label maker for spice jars and the like:
  your own text, centered and clean, with no dates.
- **Recipes and documents.** Send a recipe to an ordinary document printer
  from the Cook page or from the quick view on the Recipes page, so you can
  cook from a printed copy.

## Turning printing on

Printing runs on the standard printing system that every desktop operating
system uses, so networked, USB, and Bluetooth label printers work once they
are set up alongside ordinary document printers. Turn it on from Settings,
Printing. On a kitchen appliance the first setup takes a few minutes while it
installs the printing support for you; on a device that already has a print
system it is quick.

Printing is a per-device choice. A device without a printer simply keeps
printing off, no matter what the rest of your kitchen does.

## Adding a printer

The Printing settings have an "Add a printer" panel, so you never need the
command line:

- **Find printers** looks across your network and lists what it finds, so you
  can add one with a click. Each one gets a name box, filled in with a
  sensible default, that you can edit before adding.
- **Add by address** lets you type a printer in by its host name or IP when it
  does not turn up on its own.

Driverless (IPP Everywhere) printers, plain network printers, and Zebra ZPL
label printers are all supported, and a printer you add shows up in the label
and document printer lists right away. A Zebra label printer prints the
rendered food and spice labels, not just raw text, so an industrial label
printer works alongside the small thermal and office printers. For the pocket
Bluetooth label printers, including setting one up right from Settings, see
[Label printing hardware](hardware/label-printing.md).

## One label printer and one document printer for the whole kitchen

Printers are set once, on your main server, and every other device (a
satellite, a thin display pointed at your main server) prints through them.
That keeps one label printer and one document printer for the whole setup
instead of each screen needing its own. If you connect a printer to a
satellite instead, over Bluetooth or its network, it is shared up to the
server automatically and chosen there; a satellite's own Printing settings
show the server's printers for reference and a place to add a printer to
share up, rather than a printer choice of its own.

A device that shared its printers with the others on your network before
0.21 keeps sharing them (a label printer attached to the kitchen appliance
stays reachable from a tablet or a second screen with nothing to plug in
again), and the main server is where you point every device at the one
printer you want them all using. A Raspberry Pi or `install.sh` server set up
from 0.21 on prints for itself and does not share its printers on the network,
so connect a USB printer you want everyone to use to the main server, or add a
network printer there by its address. Either way, only the device itself can
add or change printers, and sharing stays on your local network: nothing is
exposed to the internet.

## The print service on a Docker server

On a server that runs Pantry Raider with Docker Compose, printing uses a
small print service container started with the `with-printing` profile:

```bash
docker compose --profile with-printing up -d
```

The print service publishes no port. Pantry Raider reaches it through a socket
the two share, and they sit on a private network that nothing else joins, so
no other program on the server or your network can change your printers. Add
and manage printers in Settings, Printing. The print service checks its own
configuration before each start and keeps working on the last good one if
anything is wrong, and printers you add are kept across updates.

### Where the print service image comes from

The print service runs the `olbat/cupsd` image (CUPS with the label printer
filters), pinned by digest so every install runs exactly the image it was
tested with. Its publisher now releases a separate image for each kind of
processor, so Pantry Raider keeps a copy that serves both a Raspberry Pi and
a regular PC from one name: `ghcr.io/syracuse3dprintingorg/cupsd`, tagged by
the publisher's release date. The copy holds the publisher's own images, not
a rebuild. Once that copy is in place, a later release moves the compose
file's pin from `olbat/cupsd@sha256:...` to
`ghcr.io/syracuse3dprintingorg/cupsd@sha256:...`. Updating to that release the
usual way picks it up, and your printers and their settings stay as they are,
since they live in the print service's own volume.

## Update your print service

If Settings, Printing (and an inbox item) shows **Update your print
service**, your print service still lets other programs on the server change
your printers without asking. Printing keeps working in the meantime, and
your printers and their settings are kept when you fix it. Settings, Printing
shows the steps for your kind of install:

**A server set up from a downloaded compose file.** In the folder with your
`docker-compose.yml`, download the compose file for the version you run next
to yours. The command in Settings, Printing already has your version in it;
for 0.21.0 it reads:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/v0.21.0/docker-compose.prod.yml -o docker-compose.new.yml
```

Copy anything you changed in your own `docker-compose.yml` (the data folder,
ports, passwords) into `docker-compose.new.yml`. Then replace yours with it
and restart:

```bash
mv docker-compose.new.yml docker-compose.yml
docker compose --profile with-printing up -d
```

Add the other `--profile` flags you normally start with to that last line.
Do not download the file from `main` or write it straight over your
`docker-compose.yml`: the steps above keep you on the version you run and keep
your own changes.

**A copy built from source.** Update your copy (`git pull` in its folder),
then run `docker compose --profile with-printing up -d` there.

**A print server you set up yourself.** Close its admin pages to other
programs: in its `cupsd.conf`, allow the `/admin` locations only from
`localhost`, then restart it. Pantry Raider never changes a print server it
did not set up.

A compose file from before 0.21 talks to the print service over the private
network instead of the socket (`CUPS_SERVER=cups:631`). That keeps working
until at least 0.23, so there is no rush, but moving to the new file closes
the gap for good.

Printing is also built to shrug off a bad moment: a failed job is retried and
the queue moves on, so a shared printer that is briefly unreachable no longer
quietly pauses all printing until someone re-enables the queue by hand.

## Designing your own label

Settings, Printing has a label designer on the main server. The design you
save there travels to every satellite display, so a satellite that has the
label printer prints the same layout (name, dates, QR code, and the rest) you
set up centrally, not a plain fallback; only the physical label size stays set
per device, and the design re-fits itself to whatever stock each printer is
loaded with. Pick a format (2x1, address, 2.25x1.25, 3x2, 4x6 shipping, or a
square spice label), a metric size (40x30mm, 50x30mm, 40x60mm, or 62x29mm),
or pull the sizes your own label printer actually advertises and pick one
instead of guessing the measurements. Then drag the fields you want onto a
to-scale label and drop them where you like: the name, the added and best-by
dates, the source chip, quantity, location, your own text, or a QR code. A QR
code can carry plain text or contact details, not just a web address. Give
any element an outline, drop a small decorative icon on a spice or
decorative label, and turn on **Show logo** to add a small Pantry Raider mark
to the corner of a label.

If your stock is a **square or round** die-cut label, set the label shape to
match. Square and round labels are equal on every side, so the designer keeps
the width and height the same for you. Round also draws a circle on the design
canvas, marking the printable area, so you can keep the text and the logo
inside the edge and nothing important lands in a corner that gets trimmed away.

A live preview shows the real printed label as you go. Save and reload named
designs so you can switch between label layouts without rebuilding one from
scratch, and once you save a design, every label prints your way; leave the
designer alone and labels keep the tidy default. If you navigate away with
unsaved changes, the designer asks first, so a stray tap does not lose a
layout you were working on.

## Printing recipes and other documents

A printed recipe leads with its name and a quick-facts line (prep, cook, and
total time, plus how many it serves) next to the Pantry Raider mark, then
ingredients (grouped under their section headings when the recipe has them)
and numbered steps. Long ingredient lists print in two columns and the
text sizes down when a recipe runs long, so most recipes come out on a
single sheet, in plain black and white for any printer.

Settings, Printing also has advanced document printer settings: page size,
color or black and white, and single or double sided, for recipe and
document printouts.

## A couple of everyday touches

- **A big batch asks first.** Printing more than five labels at once (for
  example after a large import) shows a quick confirmation with the count, so a
  stray tap cannot burn a roll of labels.
- **A repeat print asks first too.** Printing the same recipe again within a
  minute of the last print asks first, so a double tap does not send it to the
  printer twice.
- **A quick on-screen confirmation, not a popup.** Printing a label or a
  recipe shows a brief confirmation on screen instead of a popup you have to
  dismiss, so you know it went to the printer without it interrupting you.
- **Print from where you are.** Both the Cook page and the recipe quick view on
  the Recipes page have a Print button when printing is on, so you can send a
  recipe to your document printer without opening it first.
