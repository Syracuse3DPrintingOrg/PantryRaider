# Reducing waste: extending and adjusting best-by dates

A handful of everyday actions help you use food before it spoils, without
re-typing any dates: passing an item the sniff test to keep it a little
longer, marking a package opened so its date follows, and moving food to the
freezer (or back out again) so its best-by date follows the new storage. And
when something does not make it, tossing it keeps the record honest, so the
Waste summary can show you what keeps losing the race.

## The Expiring list

The Expiring page (under Inventory) lists everything within a week of its
best-by date, soonest first, with a colored badge showing how many days are
left or how long ago it lapsed. From each row you can mark the item consumed
(which removes it from your stock), toss it when it went bad, print a label
for it when printing is set up, and pass it the sniff test.

## The sniff test: keep it a little longer

A date on the package is a guess, and plenty of food is still good past it.
When you have checked an item and it looks and smells fine, the **Sniff test**
buttons on its row push the best-by date out without you editing anything:
**+1d**, **+3d**, or **+5d**. Pantry Raider moves every dated entry of that
product forward by that many days (counting from today when the item is already
past its date) and tells you the new best-by date. The item then drops down the
list, or off it, so it stops nagging you until the new date comes around.

The sniff test only appears where it makes sense. An item whose date is a
hard expiration (a safety date, the way its product is set up in Grocy) never
offers the keep-it-longer buttons, and its row says why instead of leaving a
silent gap; using the item up or tossing it still takes one tap. The sniff
test also meets you on the Review screen: when a scanned item is already
sitting in your stock and about to expire, its review card offers the same
+1, +3, and +5 buttons, so you can deal with the older stock without leaving
the review.

## Toss it: when something did not make it

When an item on the Expiring list is past saving, the toss button beside the
consume check removes it from stock recorded as spoiled instead of eaten. It
asks first, then confirms what happened. The Review screen's sniff-test card
offers the same way out for older stock that fails the sniff. The distinction
matters: consumed food fed someone and tossed food did not, and keeping the
two honest is what makes the Waste summary worth reading.

## The Waste summary

Those tosses add up to a Waste card at the bottom of the Expiring page: your
most-tossed items, how many times and how much of each went in the bin, and
what share of everything you used of that item was tossed instead of eaten.
The bag of spinach that keeps losing the race stops getting a pass, and the
next shopping trip can answer for it (the smaller bag, or half into the
freezer on day one).

## Mark it opened

Cracking the jar changes the clock: most food keeps far longer sealed than
open. Every in-stock item on the Inventory dashboard has a one-tap **Opened**
button: tap it the moment you open the package and the item's after-opening
shelf life takes over its best-by date, with an Opened badge on the row so a
glance tells you which package is already open. The after-opening shelf life
comes from the product's own setup in Grocy, so a product without one simply
keeps the date it had.

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
