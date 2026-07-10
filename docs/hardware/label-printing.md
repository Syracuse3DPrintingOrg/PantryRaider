# Label printing

Pantry Raider can print labels to a small Bluetooth thermal label printer, so
you can stick a name and a use-by date on leftovers, prepped ingredients, and
freezer bags. It also prints to any regular network printer for full-page
recipes. Printing is optional and off until you turn it on.

The tested Bluetooth printer is the **SUPVAN T50M** (and the T50M Pro and
T50M Plus), a pocket-size 203 dpi label printer.

## Turning on printing

Open **Settings, Printing** and turn on **Enable printing**. This is the
master switch for labels and recipe printouts; it does not by itself connect
a printer. On a Pantry Raider appliance with no printer set up yet, the panel
also offers an **Install now** button that prepares the base printing system
(the standard network path); on a plain server, follow the one-line steps
shown there instead.

## Connecting a SUPVAN T50M over Bluetooth

The T50M talks Bluetooth, not the network, so it needs a small bridge on the
device before it can show up like any other printer. This is only available
on a Pantry Raider appliance today (not a plain server); see below if you are
running a headless install.

1. In **Settings, Printing**, find the **Bluetooth label printer** panel and
   press **Set up**. The first time can take several minutes while it
   prepares the printer software; later runs are quick. The panel shows the
   progress and a status of Not set up, Setting up, Ready, or Failed.
2. Charge the T50M and load a roll of labels.
3. Once the panel shows **Ready**, turn the printer on and make sure it is
   not already paired to a phone (most of these printers only talk to one
   device at a time).
4. Press **Find printers** in the **Add a printer** panel above. The T50M
   appears there like any network printer; add it, then pick it as your
   label printer.

If **Find printers** does not turn up anything new once the panel says
Ready, make sure the printer is powered on and nearby, then try again; it
only advertises over Bluetooth while it is awake.

Tips:

- Keep the printer close to the Pantry Raider box. Bluetooth range is short,
  and a wall or a full cupboard between them will drop the connection.
- If the printer stops showing up, power it off and on and search again.
- If setup shows **Failed**, press **Set up** again; a flaky network during
  the first-time build is the most common cause.

## Headless installs (no browser access yet)

For a device you are provisioning without the setup screen, Bluetooth
printing can also be turned on from the command line by setting
`ENABLE_BLUETOOTH_PRINTING=1` before running the print stack installer, or by
adding `BLUETOOTH_PRINTING=1` to the stack's `.env` file so it is picked up
on the next run. The in-app **Set up** button above is the same thing; use
whichever is easier to reach for your setup.

## Adding other Bluetooth label printers

Only the SUPVAN T50M family is included today. Other cheap Bluetooth label
printers (for example Phomemo and Niimbot) speak their own protocols, so each
brand needs its own bridge to work with Pantry Raider. Those are not built in
yet. If you have one of these and would like it supported, let us know which
model you have so it can be looked at.

A plain USB or network printer that supports driverless printing (IPP
Everywhere) does not need any of this: it works through the standard
printing system once you enable printing, no Bluetooth setup involved.
