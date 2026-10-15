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

## Setting up from outside your home network

The wizard opens straight away on a device at home. Opened from outside your
home network, for example through a reverse proxy, a tunnel or a phone on
mobile data, a new kitchen first asks for its setup code. The code is printed
in the kitchen's log when it starts, on a line that begins
`Pantry Raider setup code`. With Docker Compose or on a Raspberry Pi, run:

```bash
sudo docker logs foodassistant-service 2>&1 | grep "setup code"
```

On Unraid, open the Docker tab, click PantryRaider and choose Logs. In the Home
Assistant add-on, open the add-on's Log tab. Use the newest code. It works once,
within 24 hours.

After the code, the wizard asks you to choose a password. Signing in from
outside your home network needs two-factor as well, so Settings, Security then
asks you to turn it on before you sign in from outside again. Without it, the
next sign-in from outside needs a new setup code.
