# Label printing

Pantry Raider can print labels to a small Bluetooth thermal label printer, so
you can stick a name and a use-by date on leftovers, prepped ingredients, and
freezer bags. Label printing is optional and off until you turn it on.

The tested printer is the **SUPVAN T50M** (and the T50M Pro and T50M Plus),
a pocket-size 203 dpi Bluetooth label printer.

## Turning on label printing

Enable printing from the setup screen (or during install). When you turn it on,
Pantry Raider sets up the printing system for you, including the bridge that lets
it talk to a SUPVAN T50M over Bluetooth.

The first time you enable printing, setup may take a few minutes: on a Raspberry
Pi it builds the SUPVAN bridge from source the first time, which is a one-time
step. Later updates reuse what is already installed, so they are quick. Give it a
few minutes and let it finish.

## Connecting a SUPVAN T50M

1. Charge the printer and load a roll of labels.
2. Turn the printer on and make sure Bluetooth is on (it advertises itself as
   soon as it is powered up and not already paired to a phone).
3. Enable label printing in Pantry Raider as described above, if you have not
   already.
4. Once the bridge is running, the printer appears as a label printer you can
   pick when you print. Print a test label to confirm it feeds and prints.

Tips:

- Keep the printer close to the Pantry Raider box. Bluetooth range is short, and
  a wall or a full cupboard between them will drop the connection.
- If your phone is already paired to the printer, disconnect it there first. Most
  of these printers only talk to one device at a time.
- If the printer does not show up, power it off and on and try the test print
  again. It only advertises over Bluetooth while it is awake.

## Adding other Bluetooth label printers

Only the SUPVAN T50M family is included today. Other cheap Bluetooth label
printers (for example Phomemo and Niimbot) speak their own protocols, so each
brand needs its own bridge to work with Pantry Raider. Those are not built in
yet. If you have one of these and would like it supported, let us know which
model you have so it can be looked at.

A plain USB or network printer that supports driverless printing (IPP Everywhere)
does not need any of this: it works through the standard printing system once you
enable printing.
