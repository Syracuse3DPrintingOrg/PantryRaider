# First run and zero-touch setup

Getting a fresh install ready used to mean logging in to Grocy (and Mealie if
you ran it) to create an API key and change a default password before Pantry
Raider could talk to them. On a new install you no longer do any of that.
Pantry Raider sets its backends up for you.

## What happens on a new install

When Pantry Raider starts against a freshly launched Grocy that still answers
to its stock sign-in, it signs in itself, creates its own API key, saves it,
and replaces the default password with a generated one. You never have to open
Grocy unless you want to. If you ever do, the generated password can be
revealed from the Inventory pane in Settings.

If you run Mealie, the same thing happens there: Pantry Raider creates its own
API token, secures the account with a generated password (revealed from the
Recipes pane), and adds a ready-made Groceries shopping list. New installs do
not set Mealie up at all, though, because recipes, the meal plan, and the
shopping list are built in. Mealie stays available as an option for people who
already run it.

## Existing installs are left alone

An install that is already set up is not touched. Nothing is reset, re-seeded,
or re-keyed, so your existing kitchen keeps working exactly as it did. If you
have an older install and want to opt in to the hands-off setup, a "Set up for
me" button in Settings does it on request.

## What you still do

The web setup wizard at `/setup` is still where you set your app password and
choose an AI provider if you want photo scanning and barcode cleanup. Those are
your choices to make, so the wizard still walks you through them with live
connection tests. What has gone away is the busywork of provisioning the
backends by hand.
