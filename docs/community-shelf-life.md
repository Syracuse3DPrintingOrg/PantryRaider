# Community shelf life and privacy

Pantry Raider suggests a best-by date whenever you add an item. Out of the box
those suggestions come from built-in rules of thumb, and optionally from AI.
Community shelf life adds a third source: what real kitchens actually chose
for the same product. This page explains exactly what that involves, in both
directions.

There are two separate switches, in Settings under Inventory & Storage:

- **Use community shelf-life estimates** (on by default) downloads a small
  aggregated table once a day and uses it when suggesting dates. It never
  sends anything about you or your pantry.
- **Share anonymous expiry corrections** (off by default) contributes your
  own date corrections back to that table. It is opt-in: nothing is shared
  unless you turn it on, and you are asked once during setup.

Neither switch needs a Forager account. Both can be changed at any time.
This page covers only shelf-life sharing; the [privacy policy](privacy.md)
covers everything else the app does with your data.

## What is shared when sharing is on

When you change a suggested best-by date while reviewing a scanned item, and
then add that item to your pantry, exactly one data point is sent. It
contains only:

- the product barcode, when the item has a real one (store-printed deli and
  scale labels are never sent, they mean nothing outside that store);
- the product name, for example "greek yogurt";
- where you stored it: fridge, freezer, pantry, or other;
- the shelf life you chose, as a number of days;
- the shelf life the app had suggested, and whether that suggestion came from
  a built-in rule, AI, or the community table.

## What is never shared

- Your name, email, or Forager account. The upload carries no sign-in at all.
- Anything that identifies your device or install: no serial numbers, no
  install id, no address. The server keeps only the daily scrambled tag
  described below, never the address itself.
- Times or dates finer than a day. A data point is just day counts; the
  server records only the calendar day it arrived.
- Your location.
- The rest of your inventory, your recipes, your photos, or anything you did
  not correct. Keeping a suggested date as-is shares nothing.
- Anything at all when the switch is off. With sharing off the app does not
  collect these points even locally, and turning sharing off discards
  anything still waiting to upload instead of sending it.

## How the community table is built

The Forager service collects the shared points and publishes only aggregate
numbers: for each product and storage place, the median shelf life and how
many senders it is based on. To tell senders apart without knowing who they
are, the server stores a short scrambled tag made from the network a point
came from and the day it arrived. The tag changes every day and cannot be
turned back into an address. Every sender gets one vote per product no matter
how many points they send, and a product appears in the table only once at
least five different senders agree reasonably well. Five senders usually
means five kitchens, but not always: the same kitchen sending on different
days counts again, and several kitchens behind one shared network count as
one.

Because the table is built from anonymous contributions, the app never lets
it make a date riskier than its own rules. For anything kept in the fridge or
on the counter, a community value can stretch the built-in estimate by half
at most, and for meat, poultry, fish, eggs and dairy it can never go past the
built-in estimate at all. A community value can always shorten a date.

## How suggestions are prioritized

When the app suggests a best-by date for a new item, sources are applied in
this order:

1. Your own expiry rules (anything you added or edited under Expiry Defaults)
   always win.
2. A community value for the product, when one exists and community estimates
   are turned on, within the limits described above.
3. The built-in rules of thumb.

An AI shelf-life estimate, when you have that feature turned on, continues to
behave as before. And a date you type yourself is always kept exactly as you
typed it.
