# Reducing waste: extending and adjusting best-by dates

Two everyday actions help you use food before it spoils without re-typing any
dates: passing an item the sniff test to keep it a little longer, and moving
food to the freezer (or back out again) so its best-by date follows the new
storage.

## The Expiring list

The Expiring page (under Inventory) lists everything within a week of its
best-by date, soonest first, with a colored badge showing how many days are
left or how long ago it lapsed. From each row you can mark the item consumed
(which removes it from your stock), print a label for it when printing is set
up, and pass it the sniff test.

## The sniff test: keep it a little longer

A date on the package is a guess, and plenty of food is still good past it.
When you have checked an item and it looks and smells fine, the **Sniff test**
buttons on its row push the best-by date out without you editing anything:
**+1d**, **+3d**, or **+5d**. Pantry Raider moves every dated entry of that
product forward by that many days (counting from today when the item is already
past its date) and tells you the new best-by date. The item then drops down the
list, or off it, so it stops nagging you until the new date comes around.

## Moving food to the freezer adjusts the date for you

Freezing buys time and thawing spends it, so Pantry Raider shifts the best-by
date when you move an item between storage places that cross that line. On the
Inventory dashboard, drag an item (or use its move menu) from Refrigerated to
Frozen and its date is recomputed from today against the item's frozen shelf
life, which for most food pushes it well out; the confirmation tells you the
new date. Move it back from Frozen to Refrigerated and the date is pulled in to
the shorter refrigerated shelf life instead.

The adjustment is careful in both directions. Freezing never shortens a date
that is already further out than the freezer shelf life would set, and thawing
never extends one: because the app does not keep a record of the pre-freeze
date, the honest cap on a thaw is the date currently on the item.

Moves that do not cross that line leave the date alone. Shuffling between Room
Temp and Pantry, or into a custom location or Other, never touches an item's
date.

Your own rules still lead. The freeze and thaw shelf lives come from the same
Expiry Defaults the app uses everywhere else, so an expiry rule you have edited
for a product is what a move uses, ahead of the community table and the
built-in rules of thumb (the full order is on
[Community shelf life and privacy](community-shelf-life.md)).
