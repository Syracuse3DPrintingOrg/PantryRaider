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
  can add one with a click.
- **Add by address** lets you type a printer in by its host name or IP when it
  does not turn up on its own.

Driverless (IPP Everywhere) printers, plain network printers, and Zebra ZPL
label printers are all supported, and a printer you add shows up in the label
and document printer lists right away. A Zebra label printer prints the
rendered food and spice labels, not just raw text, so an industrial label
printer works alongside the small thermal and office printers. For the pocket
Bluetooth label printers, see [Label printing hardware](hardware/label-printing.md).

## Sharing printers across your devices

When you turn label printing on, each device shares its printers with the
others on your network and picks up the printers they share. A label printer
attached to the kitchen appliance is then available from a tablet or a second
screen with nothing to plug in again. Sharing stays on your local network:
nothing is exposed to the internet.

Set the default printer once, on the main server: it chooses the label
printer and the document printer that every device uses by default. Any device
can still pick its own printer, which then wins on that device, and a device
with no choice of its own follows the server.

## Designing your own label

Settings, Printing has a label designer. Pick a format (2x1, address,
2.25x1.25, 3x2, 4x6 shipping, or a square spice label) or set a custom size to
match your label stock, then drag the fields you want onto a to-scale label and
drop them where you like: the name, the added and best-by dates, the source
chip, quantity, location, your own text, or a QR code. A live preview shows the
real printed label as you go, and once you save it, every label prints your
way. Leave the designer alone and labels keep the tidy default design.

## A couple of everyday touches

- **A big batch asks first.** Printing more than five labels at once (for
  example after a large import) shows a quick confirmation with the count, so a
  stray tap cannot burn a roll of labels.
- **Print from where you are.** Both the Cook page and the recipe quick view on
  the Recipes page have a Print button when printing is on, so you can send a
  recipe to your document printer without opening it first.
