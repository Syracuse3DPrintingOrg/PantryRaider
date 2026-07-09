# Install on Unraid

Pantry Raider runs on Unraid two ways. Pick the one that fits how you already
run things.

- **Just the app** (single container): you already run Grocy, or you want to
  install Grocy and Mealie from Community Applications yourself and point Pantry
  Raider at them. This is the simplest route and the one most people want.
- **The whole set** (Compose stack): install Pantry Raider, Grocy, and
  optionally Mealie together in one step with the Docker Compose Manager plugin.

Either way, Pantry Raider uses port **9284**, Grocy uses **9383**, and Mealie
uses **9285**.

## Option 1: Just the app (single container)

This is the Community Applications template. Once it is published (see
[Submitting to Community Applications](#submitting-to-community-applications)
below), you install it like any other app.

1. In Unraid, open the **Apps** tab (Community Applications).
2. Search for **Pantry Raider** and click **Install**.
3. Fill in the fields:
   - **WebUI Port**: leave at 9284 unless it clashes with something.
   - **App Data**: leave at `/mnt/user/appdata/pantryraider` so your setup
     survives updates.
   - **Grocy Address**: the address of your Grocy, for example
     `http://192.168.1.170:9383`. No trailing slash. If you do not have Grocy
     yet, install the Grocy template from Community Applications first, then
     come back and fill this in.
   - **Grocy API Key**: in Grocy, open **Manage API keys** and create one.
   - **Vision Provider** and **Gemini API Key**: a free Google Gemini key from
     aistudio.google.com is the easiest start. You can also set these later.
   - **UI Password**: set one here, or leave it blank and set it during the
     on-screen setup.
   - The advanced fields (Mealie address and key, timezone, API key) are
     optional. Show them with the **Advanced View** toggle.
4. Click **Apply**, wait for it to start, then open the **WebUI**.
5. Finish the short setup on the `/setup` page and you are done.

You do not have to fill everything into the template. Anything you leave blank
you can set on the `/setup` page after the app starts.

## Option 2: The whole set (Compose stack)

Use this if you want Pantry Raider to bring its own Grocy (and optionally
Mealie) along, all managed together.

1. From the **Apps** tab, install the **Docker Compose Manager** plugin.
2. Go to the **Docker** tab and scroll down to **Compose Manager**. Click
   **Add New Stack** and name it `pantryraider`.
3. Click the stack's cog, choose **Edit Stack**, then **Compose File**.
4. Paste in the compose file from the project:
   [`unraid/docker-compose.yml`](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/unraid/docker-compose.yml).
   It stores all data under `/mnt/user/appdata` and uses the same pinned Grocy
   and Mealie versions the project ships. Mealie starts out commented; uncomment
   its block if you want recipes and meal plans.
5. Save, then click **Compose Up**.
6. Open `http://YOUR-UNRAID-IP:9284/setup`. Enter the Grocy address
   `http://YOUR-UNRAID-IP:9383` and, if you enabled Mealie,
   `http://YOUR-UNRAID-IP:9285`.

## Which Grocy and Mealie?

Grocy and Mealie both have their own Community Applications templates already, so
you can install them from the **Apps** tab and just point Pantry Raider at them.
The Compose stack above is only for people who would rather run everything from
one place.

## Updating

- **Single container**: Unraid shows an update when a new image is published.
  Your data in `/mnt/user/appdata/pantryraider` stays put.
- **Compose stack**: pull the new images and run **Compose Up** again, or pin a
  version by setting `PANTRYRAIDER_TAG` in a `.env` next to the compose file.

## Submitting to Community Applications

These steps are for the project owner, to get the single-container template into
the Apps tab. See [`unraid/README.md`](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/unraid/README.md)
for the full detail.

1. The template already lives in this repo at `unraid/pantryraider.xml`, and its
   `TemplateURL` points at the raw GitHub copy, so Community Applications can
   keep it in sync.
2. Sign in to the Community Applications submission portal at
   `https://ca.unraid.net/submit` (the current source of truth for submission
   requirements) and add the repository, or use the older route: post in the
   Unraid Community Applications forum thread / send the moderators a private
   message with the repository URL. A moderator reviews it and adds the feed,
   usually within a couple of hours.
3. Community Applications runs automated checks (valid XML, no template
   pet-peeves). If anything is off, it shows up under **Template Errors** in CA
   settings and the app will not appear until it is fixed.
