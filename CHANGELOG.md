# Changelog

All notable changes to Pantry Raider are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

> **For releases:** use the entry below as the GitHub Release description rather
> than the auto-generated commit list, so notes stay focused on user-facing
> changes.

## [Unreleased]

### Added

- **Round and square label support in the designer.** If your label stock is a square or round die-cut, pick that shape in Settings, Printing. The designer keeps the width and height equal for you, and round labels get a circle drawn on the design canvas so you can keep the text and logo inside the printable area instead of guessing where the edge falls.
- **Show timers on a little ESP screen.** An ESP32 or ESP8266 with a small OLED or e-ink display can now pull a compact status from Pantry Raider, the kitchen timers that are running and the recipe you are cooking, and show it on the counter. It answers instantly and a finished timer shows up first, so a glance tells you what needs attention. The new ESP devices guide has the details.
- **Turn an ESP button into a timer or an action.** An ESP32 or ESP8266 running ESPHome can now send Pantry Raider a button press, and Pantry Raider runs it like a key on the Start Page: start a kitchen timer everyone in the house can see, or fire a Home Assistant action such as a light or a scene. It uses the same action names the Stream Deck does, and a sample firmware is in the new ESP devices guide. Handy for a physical timer button by the stove.
- **Read a DIY WiFi sensor as a thermometer.** Settings, Thermometers has a new "From an ESP device" option: point it at an ESP32 or ESP8266 running ESPHome (a temperature probe plus the web_server component) and Pantry Raider reads it straight off your network, no Bluetooth radio and no Home Assistant in between. Type the device address, tap Find sensors to see what it offers, and it shows up on the Timers page like any other probe, targets and ready-in estimate included. Handy for keeping an eye on a fridge, freezer, or a room.
- **Thermometers estimate when a probe will be ready.** With a target set, the probe card shows a live "Ready in ~20 min" estimate that updates as the temperature climbs, so you know roughly how long is left without hovering over the grill. It appears only while a reading is genuinely on its way to the target, and quietly disappears when the food is cooling, holding steady, or already there.
- **The demo includes a sample grill.** With no Bluetooth hardware, the demo's Temperatures section used to look empty; it now shows a clearly labeled sample grill with two live probes, one climbing toward its target with a ready-in estimate. The sample appears only in the demo and never affects a real setup.
- **An optional Beszel monitoring dashboard.** Settings, Resources can now link to a Beszel hub for history and graphs on top of the built-in live snapshot. Turn it on with the new with-beszel Docker Compose profile and paste the hub address into Settings, Resources; the built-in snapshot stays either way.

### Security

- **Closed an auth-bypass and hardened the app's headers.** Pantry Raider trusts a request from the device itself (that is what lets the on-screen kiosk skip the password), and a trick with a forwarded-for header could have let a request from elsewhere pretend to be local when a proxy sat in front of it. That path is closed. Every response also now carries standard protections against clickjacking and content-type sniffing.

### Changed

- **Clearer install choices on a Raspberry Pi.** The installer now offers three plainly-named modes: Pi Host Kiosk (the full stack plus this device's touchscreen), Pi Host Standalone (the full stack, headless: no kiosk or Stream Deck, add them later), and Pi Remote (a control surface for a server elsewhere). A Stream Deck is still picked up automatically when one is attached, so a screen without a deck carries nothing extra.
- **Setup keeps the inventory behind the scenes.** The inventory engine now configures itself during first-time setup, so the wizard no longer walks you through opening it, creating an API key, and pasting it in. The generated inventory admin sign-in is saved for you and can be revealed any time under Settings, Inventory. If you would rather point at an inventory server you run yourself, that option is still there under Advanced.
- **Setup asks you to confirm your password.** The first-run Security step now has a "Confirm password" field and will not move on until the two entries match, so a mistyped password can't lock you out.
- **Setup no longer asks you to save a master API key.** The first-run wizard used to generate a master key and tell you to write it down. It doesn't any more: when Home Assistant, a headless client, or a satellite device needs access, create a named key for it on demand in Settings, Security & Access. One key per device means you can revoke one without disturbing the others.
- **New Raspberry Pi installs answer at `pr.local`.** The device's network address is now `pr.local`, a shorter, on-brand name to open on your phone or laptop. Add a second Pantry Raider device (a satellite alongside a host, say) and the installer notices the name is taken and uses `pr-2.local` automatically, so they never clash. Existing devices keep the name they already have.
- **Pi Hosted now runs on a 2 GB Pi.** With recipes, the meal plan, and the shopping list built in (Mealie off by default), the local stack is light enough for a 2 GB Raspberry Pi 4 alongside the kiosk, so first-time setup offers Pi Hosted there instead of forcing Pi Remote.
- **Mealie is connect-only on the appliance.** The setup screens no longer offer to install or start Mealie on the device, which keeps the appliance consistent and light. Recipes live in Pantry Raider; if you already run Mealie (or start it yourself with Docker), the app still detects it and connects.
- **Reolink cameras can pop themselves up.** A Reolink camera set to pop up on a person, vehicle, animal, or doorbell visitor is now checked automatically in the background, so it appears on the kiosk on a detection without needing a Home Assistant automation.

### Fixed

- **Automatic Grocy setup no longer errors on first run.** During first-time setup the inventory backend is configured for you in the background; a routing gap made that step answer with the setup page instead of a result, so it showed "Unexpected token '<'". The setup requests now return properly, and the automatic configuration completes on its own once the inventory service is up.
- **The setup QR code shows on the kiosk again.** The "finish setup from your phone" code was sized in physical millimetres, which the on-device kiosk browser rendered as a broken image; it now scales correctly on screen.

## [0.17.21] - 2026-07-10

### Added

- **Focus the Timers page on what you are cooking.** A toggle shows just timers, just thermometers, or both, and the page remembers your choice.
- **Pick a doneness instead of a number.** Setting a probe target now offers presets like Beef medium-rare, Chicken, or Pork, with your own custom temperature still available. If a recipe is on the line and names a target, one tap fills it in.
- **Home Assistant knows when a probe hits its target.** Alongside the on-screen alert, Pantry Raider sends a Home Assistant event when a probe reaches its target, so your automations can flash a light, announce it, or push a notification.
- **A recipe photo and cook time in the editor.** Add or replace a recipe's photo while editing, and set a cook time alongside prep and total time.
- **Cameras pop up on a detection.** Turn on, per camera, an on-screen pop-up when a person, vehicle, animal, or a doorbell visitor is detected, from Home Assistant or a Reolink camera. Reolink doorbells and two-way-talk-capable models are flagged as such.

### Changed

- **Clearer low-battery warnings.** Thermometers and label printers that report their battery now show a consistent low-battery badge on the Timers page and in Settings, so you can swap batteries before they die mid-cook. Devices that do not report a battery simply do not show one.
- **Our Picks match the real build.** The recommended hardware list is the actual parts we use: a Raspberry Pi 4 (4GB), a Hoysund 7-inch touchscreen, the 15-key Elgato Stream Deck MK.2, the Waveshare barcode scanner, and a Supvan T50M Pro label printer.

### Fixed

- **A sleeping label printer asks you to turn it on.** A Bluetooth label printer that has auto-powered off after inactivity no longer fails a print silently or hangs; it tells you to turn the printer on and try again.

## [0.17.20] - 2026-07-10

### Added

- **Edit your own recipes in place.** Recipes in your library can now be opened and edited, not just created: change the name, description, servings, prep and total time, ingredients, and steps, and it saves straight back to your library. (Recipes kept in Mealie are still edited there.)
- **Use a label size your printer supports.** Under Printing, Label size, pull the sizes your label printer actually advertises and pick one, instead of guessing the measurements.
- **More label design options.** Give a design element an outline, save and reload named label designs, and put a small icon on a decorative label. QR codes on a label can now carry plain text or contact details, not just a web address.

### Changed

- **Recipe printouts fit on one page with a cleaner header.** A printed recipe leads with its name and a quick-facts line (prep, cook, and total time, plus how many it serves) next to the Pantry Raider mark, then ingredients and steps. Long ingredient lists print in two columns and the text sizes down when a recipe runs long, so most recipes come out on a single sheet, in plain black and white for any printer.

### Fixed

- **The kitchen display runs smoother.** On a Pi kiosk, the browser was falling back to software rendering because it no longer accepted an old graphics setting, which made the screen sluggish. It now uses the Pi's graphics hardware, so pages and transitions are quick. Existing devices pick this up on their next update.

## [0.17.19] - 2026-07-10

### Added

- **A food-themed note on the weather page.** The weather page shows a short, playful line based on the current forecast: hot enough to fry an egg on a scorcher, soup weather on a cold day, a nudge that the grill might get rained on.
- **Discover grills for Home Assistant thermometers.** If your grill or smoker exposes several probes to Home Assistant, Settings, Thermometers now has a Discover grills list that groups those probes into one device and adds them all at once, instead of adding each probe by hand.
- **A Pantry Raider logo on printed labels.** Turn on Show logo under Printing to add a small black-and-white mark to the corner of your food labels. Off by default.
- **Advanced document printer settings.** Choose page size, color or black and white, and single or double sided for recipe and document printouts.

### Changed

- **The label designer warns before you leave with unsaved changes**, so a stray tap does not lose a layout you were working on.

## [0.17.18] - 2026-07-10

### Changed

- **Label size and the label designer live on the main server.** They describe the printer's label stock, so a satellite no longer shows them; it inherits them from the server and prints through it. The satellite's Printing settings stay focused on connecting a printer to share up.

## [0.17.17] - 2026-07-10

### Changed

- **Printers are set once, on your main server, and satellites print through them.** A satellite (a thin display device) no longer picks its own label or document printer. It prints labels and recipes on the main server's printer, so there is one label printer and one document printer for the whole setup. A printer you connect to a satellite (over Bluetooth or its network) is shared up to the server and chosen there. The satellite's Printing settings now show the server's printers for reference instead of a per-device choice.

## [0.17.16] - 2026-07-10

### Fixed

- **Labels print with their content instead of coming out blank.** A label was being sent to the printer without its size, so the printer put it on the wrong-sized page and fed a blank label. Each label now carries its stock size, so a Bluetooth label printer (and other label printers) prints the whole label. Set your label size to match your stock (Settings, Printing, Label size) for the best fit.

## [0.17.15] - 2026-07-10

### Added

- **Named staple-food timers.** The Timers page has a Common foods row: one tap starts a labeled countdown for a soft or hard egg, pasta, rice, steamed veg, a baked potato, or a tea steep, alongside the plain minute presets.
- **Metric label sizes.** Pick 40x30mm, 50x30mm, 40x60mm, or 62x29mm label stock from the size dropdown, next to the existing inch sizes.
- **A confirmation before printing the same recipe twice.** Print a recipe again within a minute of the last print and Pantry Raider asks first, so a double tap does not send it to the printer twice.
- **Hide Home Assistant sensors with no current reading.** The Home Assistant entity picker under Settings, Thermometers has an "only show sensors with a current reading" option, on by default, so a big HA install does not bury your live probes under a long list of unavailable ones.

### Changed

- **Printing a label or recipe shows a quick on-screen confirmation** instead of a popup you have to dismiss, so you know it went to the printer without it interrupting you.
- **Clearer message when an update comes back on the same version.** That usually means a just-released update is still building its image, so the app now says you are on the newest it could pull and to wait a moment and try again, instead of the vague "may already have been up to date".
- **On a satellite, the Bluetooth printer points you to the main server.** A satellite adds shared printers on the server, so its Ready state now sends you there to add the printer instead of a Find button that did nothing on that device.

### Fixed

- **Deli and store barcodes no longer import as the wrong item.** A barcode from a deli scale or a store-assigned label is never a real product code, so instead of the AI guessing at a name (a deli-meat scan once came in as bananas), Pantry Raider recognizes these and asks you to take a photo of the item so it can be added accurately.
- **The Cook wizard guided finder no longer shows recipes that do not match your answers.** Picking a cuisine or dietary need now narrows your own recipe library the same way it narrows web results, and matches are ranked by fit instead of your saved recipes always showing first.

## [0.17.14] - 2026-07-10

### Changed

- **The Bluetooth printer setup reads as clear steps now.** The panel shows the one step you are on instead of a lone button that quietly re-ran a several-minute install: set the bridge up, and once it is Ready it says so and gives you a Find my printer button that jumps straight to the search, so the two steps connect. Re-running setup is a small link, not the main button, so it stops looking stuck. On a satellite it says plainly that the printer works there and is shared to your main server.

## [0.17.13] - 2026-07-10

### Changed

- **Name a printer as you add it.** When you add a printer from Find printers, each one now has a name box you can edit before adding, filled in with a sensible default and kept to letters, digits, dashes, and underscores as you type. A shared Bluetooth label printer from another device now adds cleanly instead of being rejected for its name, and you can call it whatever you like.

## [0.17.11] - 2026-07-10

### Added

- **Print to a satellite's label printer from the main server.** When a satellite device (a thin display pointed at your main server) hosts a Bluetooth label printer, that printer now appears in the server's Find printers as "... (on <device>)". Add it there and the server prints to it across your network, so one label printer can serve the whole setup instead of only the device it is plugged into.

## [0.17.10] - 2026-07-10

### Fixed

- **The Bluetooth printer Set up button no longer spins forever.** After setup finished, the button kept showing its "Setting up..." spinner even though the printer was ready and the button was usable. It now returns to "Set up" once setup settles.
- **Unknown barcodes no longer come back as the wrong product.** When a scan was not in the Open Food Facts database and the optional barcode AI guess was on, the app could invent a confident but wrong name from the barcode digits (a Stella Artois scan came back as "Campbell's"). The guess now only names a product the model actually recognizes, marks anything it does return as an unverified guess, and otherwise reports the barcode as not found so you can name it yourself.

## [0.17.9] - 2026-07-10

### Added

- **Satellite thermometers show up on the main server.** A satellite (a thin display device pointed at your main server) with a Bluetooth radio now sends the thermometers it reads to the server, so they appear on the server's Timers page and on the satellite's own kiosk, which shows the server's pages. Add and manage those probes from either screen; the server holds the list.

## [0.17.8] - 2026-07-10

### Fixed

- **The shopping list works on Grocy again.** On a setup that keeps its shopping list in Grocy, the list came back empty and the page kept trying to reach Grocy, because Pantry Raider was asking Grocy for the wrong thing. It now reads and writes the correct Grocy shopping list, so your items, adds, checks, and clears all show up.

## [0.17.7] - 2026-07-10

### Fixed

- **Bluetooth label printers that show up as a serial number now connect.** Some Supvan printers, including the T50M Pro, announce themselves over Bluetooth as a bare serial (like "T0148...") rather than a friendly name, and the printer bridge was skipping them, so Find printers never saw them. The bridge now recognizes those serials and adds the printer on its own; power it on near the device and it appears in Find printers, ready to add.

## [0.17.6] - 2026-07-10

### Changed

- **The Timers page shows temperatures as their own section, with probe labels.** The page is now Timers up top and Temperatures below, split by a clear divider, and the browser tab reads Timers & Temps. Each probe carries its role (a TempSpike reads Internal and Ambient), and when you have set no target of your own, a Govee grill's on-device alarm shows as the probe's setpoint.

## [0.17.5] - 2026-07-10

### Fixed

- **The thermometer rename button works.** The pencil next to a thermometer opened nothing on 0.17.4; it now prompts for a name as intended.

## [0.17.4] - 2026-07-10

### Added

- **Name your thermometers and label each probe.** Any added thermometer can be renamed to something you recognize (Grill, Smoker), so a probe that only broadcasts a code no longer shows a bare address. Each probe now shows what it measures: a two-lead probe like the ThermoPro TempSpike is labeled Internal (the tip in the food) and Ambient (the pit or oven air) on its own, and you can override any probe to Internal, Ambient, or Food when the guess is wrong or you have moved a lead.
- **Govee grill setpoints come across on their own.** When you set an alarm temperature on a Govee grill thermometer itself, Pantry Raider reads it off the broadcast and shows it as that probe's target, so you do not have to type it in twice. Your own target, if you set one in the app, still wins and is what drives the alert.

## [0.17.3] - 2026-07-10

### Added

- **Set up a Bluetooth label printer right from Settings.** Settings, Printing now has a Bluetooth label printer panel for the Supvan T50M family: press Set up, watch the progress, and when it reads Ready, power the printer on and it appears under Find printers like any other printer. New devices download a ready-made printer bridge in seconds; a device that cannot is honest about the few minutes it needs to prepare.
- **ThermoPro TempSpike and Govee grill thermometers now read live.** The Bluetooth reader understands the TempSpike (TP960 family, tip and ambient) and Govee grill thermometers (H5182 and siblings, with their alarm targets), straight from their broadcasts with no pairing. Govee room hygrometers are recognized and kept out of the probe list, so a house full of them stays out of your way. A nearby probe the app does not support yet shows up as "seen nearby, not supported yet" instead of disappearing.
- **Printing, Thermometers, and Resources are on the Settings overview.** The overview cards now include all three, so the newer sections are findable without hunting through the side menu.

### Fixed

- **The Bluetooth reader turns the radio on itself.** On a device where Bluetooth was switched off, the thermometer reader saw nothing and said nothing. Setup and every reader start now switch the radio on, and if it still is not available, the Thermometers section says plainly that Bluetooth is turned off on this device.

### Changed

- **The docs caught up with everything new.** New user guides for printing and the label designer, Bluetooth thermometers, the Cook page and wizard, the Resources page, and the zero-touch first run, plus refreshed feature lists across the site and README.

## [0.17.2] - 2026-07-09

### Added

- **A Thermometers section in Settings.** Bluetooth thermometers now have a proper home under Settings: turn the feature on, see at a glance whether the reader is connected, manage your probes and targets, add a thermometer by address, and on a Pi set the reader up with one click. A server explains plainly that the reader runs on the machine itself and needs a Bluetooth radio.
- **Read thermometers through Home Assistant.** If Home Assistant already sees your Bluetooth thermometers (including through ESPHome Bluetooth proxies around the house), Pantry Raider can read them from there: pick the temperature entities in Settings and they show up on the Timers page with targets and alerts, exactly like a directly connected probe. No Bluetooth radio needed on the Pantry Raider machine, which makes this the natural path for a server install.

## [0.17.1] - 2026-07-09

### Added

- **Bluetooth kitchen thermometers.** Pantry Raider now reads BLE meat and probe thermometers directly, all locally with no cloud account: Inkbird (IBT-2X/4XS/6XS), ThermoPro, Combustion Inc, and ThermoWorks BlueDOT. Live probe temperatures appear on the Timers page in big kitchen-readable numbers with battery state, and you can set a target per probe (above or below) that pops an on-screen alert exactly once when it is reached. A thermometer in range simply shows up as ready to add. The reader is an optional add-on on the appliance; turn it on and the rest happens by itself.
- **A Resources page shows what your device is doing.** Settings now has a Resources section with live processor use (overall and per core), memory, storage for the app data and the system, temperature, uptime, and on a Pi the power and throttling state, refreshed every few seconds while you watch.
- **Your own photos as the screensaver.** Point the screensaver at a folder of photos on the device, an Immich album, or a list of image links, and it plays those instead of the built-ins. Google Photos and iCloud do not offer reliable access for third-party apps, and the settings say so plainly rather than pretending.

### Fixed

- **Settings stay put after saving.** Saving a setting no longer bounces you back to the top of Settings: every save now returns you to the exact section you were in, including the ones that need a page refresh to apply.
- **Scanned items get honest label chips too.** Best-by dates on items that came in through a barcode scan or receipt now remember whether the AI or the built-in rules set them, so printed labels show the right "est." or "AI" chip for those as well.
- **No more duplicate printers in the lists.** With printer sharing on, a device could see its own shared printers echoed back as duplicates. The printer lists now hide those echoes while keeping real printers from other devices.

## [0.17.0] - 2026-07-09

### Changed

- **Pantry Raider no longer needs Mealie.** Recipes, the meal plan, and the shopping list now all work with nothing but Pantry Raider and Grocy. The meal plan is stored in the app, and the shopping list lives on Grocy alongside your inventory, so scanning, quick-add buttons, the Stream Deck count, and "Add to cart" from a recipe all keep working exactly as before. If you already use Mealie, nothing changes until you choose to migrate: your recipes, meal plan, and shopping list stay on Mealie, and one click copies your library into Pantry Raider whenever you are ready. New installs no longer set up Mealie at all; it remains available as an option for people who already run it.
- **The Recipes settings show where your library lives.** A new card at the top of the Recipes settings says plainly whether your recipes are stored in Pantry Raider or come from your Mealie, with the copy-into-Pantry-Raider button and a choice of where the shopping list lives.

## [0.16.18] - 2026-07-09

### Added

- **Grocy and Mealie now set themselves up.** On a new install, Pantry Raider signs in to a freshly started Grocy itself, creates its API key, saves it, and changes the default password to a generated one you can reveal in Settings whenever you want it. If you run Mealie, the same happens there, including a ready-made Groceries shopping list. You never have to log in to either one unless you choose to. Installs that are already set up are left completely alone, and a "Set up for me" button lets an existing install opt in.
- **Your recipes now live in Pantry Raider itself.** Recipes are stored right in the app: importing from a link, photo, PDF, or file, searching, cooking, suggestions, and sharing all work with nothing else installed. Link imports use the same well-proven reader Mealie uses, so quality does not change. If you already keep recipes in Mealie, nothing moves until you press "Copy into Pantry Raider", which copies everything over (photos and cook history included) and never changes your Mealie. Mealie remains fully supported as an optional connection; meal plan and shopping still use it for now.

## [0.16.17] - 2026-07-09
### Added

- **Print a recipe from the Recipes page too.** The recipe quick view on the Recipes page now has a Print button, like the one on the Cook page, so you can send a recipe to your document printer without opening it first. It shows when printing is on.
- **A Cook wizard walks you to tonight's recipe.** A new "Launch cook wizard" button on the Cook page opens a step-by-step, touch-friendly guide. Know what you want? Type it and search your recipes and the web, or have the AI create it. Not sure? Tap through a few big-button questions (cuisine, dish type, dietary needs, every step skippable) and land on suggestions from your own library and the web, with an option to ask the AI to invent one. Every result opens the usual quick view, so Cook, Add to cart, and Print all work from inside the wizard.
- **The app suggests buttons based on how you actually cook and shop.** The Start Page and Stream Deck editors now offer a Suggested section: groceries you have bought three or more times in the last month appear as ready-made quick-add shopping buttons, and a recipe you cook often appears as a Cook shortcut. Each suggestion says why it is offered, adds with one tap, and can be dismissed for good. Suggestions follow your real history, so they keep up as your habits change.
- **Labels are honest about estimated dates.** Printed food labels now show the small "est." or "AI" chip only when the best-by date really came from the built-in shelf-life rules or the AI. The app remembers how each date was set, and a date you edit yourself simply prints with no caveat.

### Fixed

- **Tapping a device warning now takes you to the right place.** The on-screen warning for things like a hot or underpowered device used to drop you at the top of Settings. It now opens the Device health section directly, where the details and controls live.
- **Printing a big batch of labels asks first.** Printing more than 5 labels at once (for example after a large import) now shows a quick confirmation with the count, so a stray tap cannot burn a roll of labels.
- **A Pi with both Ethernet and Wi-Fi connected now answers on both addresses.** Newly flashed devices configure the network stack so connecting to either address works, instead of one of them silently timing out.
- **On-screen quick-add shopping buttons now work.** A quick-add shopping key on the Start Page used to only work from a physical Stream Deck; tapping it on screen now adds the item to your shopping list too.

## [0.16.9] - 2026-07-09

### Fixed

- **Printing setup repairs a broken print configuration on its own.** If the print system's configuration ever ends up in a state it cannot start from, setting up or updating printing now detects that and restores a known-good configuration, so the print service always comes back and your printers stay reachable.

## [0.16.8] - 2026-07-09

### Added

- **Design your own labels.** Settings, Printing now has a label designer. Pick a format (2x1, address, 2.25x1.25, 3x2, 4x6 shipping, or a square spice label) or set a custom size, then drag the fields you want onto a to-scale label and drop them where you like: the name, the added and best-by dates, the source chip, quantity, location, your own text, even a QR code. A live preview shows the real printed label as you go, and once you save it, every label prints your way. Leave it alone and labels keep the tidy default design.

### Fixed

- **Turning on printing no longer stops the print service from starting.** On a kitchen appliance, enabling label printing could write a bad line into the print system's configuration, which left it unable to start, so nothing could print and no printers showed up. Printing now sets itself up cleanly, and a device that hit this heals itself the next time it updates.

## [0.16.6] - 2026-07-09

### Changed

- **The printed labels look sharper and are easier to read at a glance.** Food labels have a cleaner layout now: the name sits up top, a thin rule sets it off, the best-by date is shown large as the thing that matters most (with a small chip noting whether it was typed, estimated, or worked out by the AI), and a tidy footer carries the added date and any note. Dates read the friendly way too, like "Jul 23, 2026" instead of "2026-07-23".

### Added

- **Add a printer right from Settings, no command line needed.** The Printing settings now have an "Add a printer" panel. "Find printers" looks across your network and lists what it finds so you can add one with a click, and "Add by address" lets you type in a printer by its host name or IP when it does not show up on its own. Driverless (IPP) printers, plain network printers, and Zebra ZPL label printers are all supported, and a newly added printer shows up in the label and document printer lists right away.
- **Zebra label printers now print your labels.** A Zebra ZPL label printer (added as the "Zebra label printer" type) prints the rendered food and spice labels, not just raw text, so an industrial label printer works alongside the small thermal and office printers.

### Changed

- **Turning on label printing is quicker.** Setting up printing for a network printer no longer waits on the Bluetooth label-printer support to build first. The Bluetooth bridge is now a separate opt-in, so the everyday network-printer setup finishes fast, and Bluetooth label printers are added only when you ask for them.

### Fixed

- **The Printing page now guides you to set up a printer when none is installed.** On a device with the print client but no actual print system running, the page reported printing as ready and showed an empty printer list with no way forward. It now checks whether a print system is genuinely reachable, so the "Install now" prompt appears when it should and the list only claims to be ready when it is.
- **The main server can now save its default printer.** The fleet default label and document printer chosen on the main server were being dropped on save, so the choice never stuck. They now save correctly and other devices inherit them as intended.

### Added

- **A printer plugged into one device now prints from all of them.** When you turn label printing on, each device shares its printers with the others on your network and picks up the printers they share, so a label printer attached to the kitchen appliance is available from a tablet or a second screen without plugging anything in again. Sharing stays on your local network. Nothing is exposed to the internet.
- **Set the default printer once, on the main server.** The main server now chooses the label printer and the document printer that every device uses by default, so you set it up in one place instead of on each screen. Any device can still pick its own printer, which then wins on that device, and a device with no choice of its own follows the server. This rides the same setup other devices already inherit from the server.

### Added

- **A one-command way to fill a demo with sample groceries.** Setting up a demo or trying Pantry Raider out is quicker with a stocked kitchen: a new helper (`scripts/seed-demo-inventory.py`) adds a curated set of everyday ingredients, enough that the "What Can I Cook" page comes back with real recipe ideas right away. Run it on the device and it needs no password; point it at a remote instance with an API key if you prefer. Turn demo mode off while you seed, then switch it back on.

### Fixed

- **The Printing panel now shows "On" as soon as you save it.** After turning printing on and saving, the little status pill in the panel header kept reading "Off" until you reloaded the page, which made it look like the switch had not taken. It now flips to "On" the moment the save succeeds, so the panel matches the switch.

## [0.16.0] - 2026-07-08

### Added

- **Print labels for your pantry, right from Pantry Raider.** You can now print a label for any item straight from your inventory, or a whole batch of labels in one go when you bring in new stock. A food label shows the name, the date you added it, and the best-by date, with a small note next to the best-by telling you how that date was set: you typed it, it came from the built-in shelf-life guide (shown as "est."), or the AI worked it out (shown as "AI"). Labels default to a tidy 1 by 2 inch size, and you can set your own size for whatever label stock your printer uses. There is also a decorative label maker for spice jars and the like: your own text, centered and clean, with no dates. And you can send a recipe to a regular printer when you would rather cook from paper.
- **Works with the label printer you already have, including cheap Bluetooth ones.** Printing runs on the standard printing system every desktop OS uses, so networked and USB label printers (Zebra and the common clones) and ordinary document printers work once they are set up. Inexpensive Bluetooth label makers are supported too, starting with the SUPVAN T50M and its Pro and Plus siblings, which print over Bluetooth once label printing is turned on. Printing is off until you want it: turn it on from Settings, which installs the printing support for you (on a kitchen appliance the first setup takes a few minutes while it gets everything ready), and nothing about your existing setup changes until you do.

## [0.15.35] - 2026-07-08

## [0.15.34] - 2026-07-08

### Fixed

- **Your own pantry items now find web recipes, even specialty products.** The "What Can I Cook" page could come back with an empty "From the Web" section when your fridge was full of specialty items like "Shredded Swiss Cheese", "Pickled Red Onions with Peppers", or "Boneless Skinless Chicken Breast". The web recipe search now reduces those product names to the base ingredient a recipe catalog understands ("cheese", "onions", "chicken"), and looks deeper into your stock so a common staple still gets matched. Real pantries with branded, prepared, or unusual items now surface web recipe ideas instead of nothing.
### Fixed

- **Press a running timer on the Stream Deck to add a minute.** Tapping a running kitchen timer key used to restart it from the top, wiping out the time already counted down. Now a quick press adds a minute to the time remaining, so you can nudge a timer along without losing your place. A long press still cancels it, and pressing a finished timer still stops the alert.

## [0.15.33] - 2026-07-08

### Fixed

- **Kitchen panel navigation fits a narrow screen.** On a small kiosk panel in portrait, the top navigation row could run off the right edge and clip the last buttons. It now tucks the tab row away on a narrow panel, and you navigate with the on-screen menu, the floating nav, or the Stream Deck.

## [0.15.32] - 2026-07-08
### Fixed
- **What Can I Cook now shows web recipes again, not just your own.** The Cook page pulls ideas from the free web recipe source (TheMealDB, or Spoonacular if you add a key) as well as your own Mealie library, but a change had let a well-stocked Mealie library crowd the web results out of view entirely. Web recipes now have their own "From the Web" section that always shows what the web turned up against your current stock, no matter how many of your own recipes match. Nothing configured is needed for this: the free source works out of the box, and if the web is unreachable or turned off, your own recipes still list exactly as before.

### Changed
- **Tapping a recipe on the Cook page now opens it right in the app, instead of jumping out to Mealie.** Clicking a recipe (yours or a web one) opens a quick view inside Pantry Raider with its photo, ingredients, and steps, so you can read it without leaving the kitchen screen. From that popup you can Cook it (make it the current recipe) or Add to cart (put what you are missing on your shopping list), and Open in Mealie (for your own recipes) or View source (for a web recipe) is now a separate, deliberate button rather than the default click. A web recipe simply shows what detail it has, plus the View source link.
### Added
- **A gentle heads-up when a new version is out.** When a newer version of Pantry Raider is available, a small banner now appears at the bottom of the screen the next time you open the app, in a web browser or on your phone. It tells you which version is ready and gives you two choices. On a kitchen appliance, Update now applies the update in place and the app restarts on its own; on a regular server or a phone browser, View update opens the release page so you can read what changed and update from there. Dismiss tucks it away, and it stays gone for that version, so it never nags. If a later version comes out, it lets you know once more. The check is light on purpose: it looks at most once every few hours per device, so it never slows the app down or hammers anything in the background.
- **The shopping list's "By name" quick-add now suggests matching foods as you type.** On the Add page, typing into the Shopping tab's By name box now offers your existing Mealie foods that match what you have typed so far, so you can pick a name in a tap instead of spelling it out. The suggestions come straight from Mealie and stay out of your way: keep typing to add a brand-new item that is not on the list and it still adds exactly as before. If Mealie is not set up, or a lookup is slow, the box simply stays a plain text field with no suggestions.

## [0.15.30] - 2026-07-07
### Added
- **The kitchen screen now warns you on-screen when the Pi has a power or heat problem.** If the Raspberry Pi detects under-voltage, overheating, or that it has slowed itself down to cope, a clear alert now pops up right on the kitchen display, even when Home Assistant on-screen events are turned off. Under-voltage usually means the power supply or cable cannot keep up, which is the most common cause of a Pi acting up, so catching it on the screen saves a lot of guesswork. The alert appears once when the condition starts rather than nagging every minute, and shows again only if it clears and comes back. The small warning triangle in the menu bar and the Status page still list these conditions too, and now refresh every minute so they stay current.

## [0.15.29] - 2026-07-07

## [0.15.28] - 2026-07-07
### Changed
- **The Stream Deck sleep logo now follows the deck's own idle time, not the kitchen screen.** The friendly Pantry Raider raccoon that fills the deck keys is now tied to "Blank after idle (min)" for the deck itself: when the deck sits untouched for that long, it lights the logo across the keys instead of going dark. Press any key (or touch the kitchen screen) to bring the buttons right back. Turn the setting off to have the deck blank fully to black at idle, as before. This decouples the logo from the kitchen display, so the deck shows the mark whenever it goes idle on its own, whether or not the screen is asleep. The setting is now labeled "Show logo when deck goes idle."

## [0.15.27] - 2026-07-07

## [0.15.26] - 2026-07-07

## [0.15.25] - 2026-07-07
### Fixed
- **The Stream Deck now shows its Pantry Raider logo when the kitchen screen sleeps under the screensaver.** The deck is meant to show a friendly raccoon logo across its keys while the kitchen screen is asleep, so a dark kitchen shows the brand mark instead of stale keys. It only did this when the screen powered fully off, not when the screen dimmed to its floating screensaver, so on panels that use the screensaver the logo never appeared. Now the deck shows the logo whenever the kitchen screen sleeps by either path, and returns to its keys the instant the screen wakes from a touch or a deck press. If you have turned the deck's sleep logo off, it still stays off.
- **The screensaver clock and pages no longer run off the right edge on a rotated screen.** On a screen turned to 270 degrees the screensaver's corner clock was clipped off the right side during the photo slideshow, and some pages leaned slightly past the right edge too. The clock now stays fully on screen at every rotation, and kitchen pages are kept from spilling sideways on the panel.
- **Opening the screensaver by hand now keeps it up until you actually wake the screen.** Starting the screensaver from the timers menu, the settings Test button, or a Stream Deck key would show it for a moment and then dismiss it on its own, because it mistook the tap that opened it for a fresh wake. It now stays up until you genuinely touch the screen or press a deck key.

### Changed
- **The flying-toaster screensaver toasters now have two wings and a solid 3D chrome look.** Following the After Dark original more closely, each toaster is a chrome toaster seen at a slight angle, with a lit front, a shaded side, a top with the toast slots, and a feathered wing flapping on each side, so it reads as a real metallic object rather than a flat cutout. The occasional Pantry Raider pink toaster is still in the mix, and everything is still drawn on the screen with no downloads so it works offline.

## [0.15.24] - 2026-07-07
### Added
- **Two retro screensavers, including flying toasters.** The kitchen screensaver has two new styles alongside the bouncing logo and the photo slideshow. Flying toasters is a tribute to the classic After Dark saver: winged chrome toasters and slices of toast sail across a black screen in layers, wings flapping, with the odd Pantry Raider pink toaster mixed in. Starfield streams stars out from the center at warp. Pick either one under Settings, Screen, Screensaver style, and the Test button previews it right away. The clock still shows and hops around to guard against burn-in, your kitchen timers still float over the top, and any touch, key, or Stream Deck press brings the page back. Both are drawn on the screen with no downloads, so they work offline, and the star and toaster counts are kept light so they run smoothly on a Raspberry Pi.

## [0.15.23] - 2026-07-07

## [0.15.22] - 2026-07-07

## [0.15.21] - 2026-07-07
### Added
- **The kitchen screen now confirms Stream Deck actions that do not change the page.** Some Stream Deck keys do their work behind the scenes: a shopping quick-add drops an item on your list, and the scanner-mode key switches how scans are routed, all without opening a new page on the kitchen screen, so it was easy to wonder whether the press landed. Now these show a brief confirmation on the screen, like "Added milk to shopping list", "Scanner: Audit", or "Eggs timer started", that clears itself after a few seconds. It shows even if you have Home Assistant on-screen notifications turned off, because it is feedback for your own button press, not a Home Assistant alert.

### Fixed
- **A Stream Deck press now wakes the kitchen screen out of the screensaver.** Pressing a Stream Deck key already woke a sleeping kitchen display, but if the screen had dimmed to its floating screensaver clock, the screensaver stayed up until you touched the screen itself. Now a press on the Stream Deck clears the screensaver on the kitchen screen too, the same way touching the screen already wakes the Stream Deck, so either surface brings both back.

## [0.15.20] - 2026-07-07

## [0.15.19] - 2026-07-07
### Changed
- **Guidance to use a high-endurance SD card.** The docs now recommend a high-endurance or industrial microSD card, especially for a full host where Grocy and Mealie write constantly, and explain why a cheap USB thumb drive is not a substitute.


## [0.15.18] - 2026-07-07
### Fixed
- **The Stream Deck reconnects on its own after an unplug.** If the Stream Deck is unplugged and plugged back in, or a USB hiccup drops it for a moment, it now comes back by itself with the same page, brightness, and live faces (clock, weather, timers), so there is nothing to reset from the Stream Deck settings. If no Stream Deck is attached when the controller starts, it quietly waits for one to be plugged in instead of failing and retrying in a loop.
### Added
- **Install Pantry Raider on Unraid.** There is now an Unraid Community Applications template, so you can install Pantry Raider straight from the Apps tab and point it at a Grocy (and optional Mealie) you already run. If you would rather run the whole set on Unraid, a ready-made Compose file installs Pantry Raider, Grocy, and optional Mealie together through the Docker Compose Manager plugin, with all data kept under your appdata share. See the new "Install on Unraid" page in the docs for the step-by-step.
### Added
- **Scanned recipe PDFs are now read with AI.** Importing a PDF that is really a scan or a page of images (no text to pull out) used to dead-end with "try a photo instead." Now Pantry Raider turns the PDF's pages into pictures and reads them with the AI you have set up, the same way importing from a photo works, and hands you the recipe to check over and save. A recipe that runs across a couple of pages is stitched back together into one. As with a photo import, the page images go only to the AI provider you chose. If you have not set up AI yet, a scanned PDF still points you to turn it on (or to use a photo), since reading images needs it.
### Added
- **A Raspberry Pi appliance protects its SD card and sizes itself to your board.** A new install on a Pi now guards the SD card against the two things that wear it out or corrupt it: everyday logging moves into memory, the filesystem stops writing a timestamp every time a file is read and batches its writes, and swapping moves off the card into compressed memory (zram) instead. Updates were also made power-loss-safe: the new version is downloaded in full before anything switches over, so pulling the plug mid-update always leaves a working device on either the old or the new version, never a broken mix. On top of that, the installer now matches the stack to your board's memory: a 2 GB Pi gets Pantry Raider and Grocy and leaves Mealie off (you can add it later on a 4 GB or larger board), while a 4 GB or larger board runs the full kitchen. A new documentation page, "Pi reliability and memory tiers," explains every change, which stack to run per board, and the exact steps to bring an already-running Pi up to date without a reflash. None of this affects a server install on a mini PC or NAS.

## [0.15.17] - 2026-07-07
### Changed
- **Switch your kitchen between self-hosted and Forager, clearly and safely.** The Forager settings now spell out the two ways to run your kitchen and let you move between them any time. "Switch to self-hosted" runs everything on your own network with no account: it keeps all of your inventory, recipes, settings, and your device password, and only turns off Forager scanning and reaching your kitchen from away. It also makes sure scanning does not silently break, so after switching you are pointed to set up your own AI on the Scanning page (or leave it off). Connecting to Forager is the plain reverse: it adds hands-off photo, receipt, and barcode scanning and optional remote access on top of your local kitchen. Either direction is a settings change, never a move: your kitchen data never leaves your device. A note in the same place reassures you that your kitchen keeps working on your own network (inventory, recipes, the kitchen screen, and adding items by hand) even if Forager or the internet is down.

## [0.15.16] - 2026-07-07
### Added
- **Back up your kitchen to Forager, with Premium.** If your kitchen is signed in to a Premium Forager account, Settings, Backups now offers "Back up to Forager" and "Restore from Forager". A backup saves this app's settings and data to your account so a lost or replaced device can be brought back without handling a file. Restoring pulls your latest backup down and puts it in place, and it still asks for your app password first, the same as restoring from a file. Forager keeps your few most recent backups. Your account page shows how many backups you have, when the last one was made, and how much space they use. The everyday download-a-backup button keeps working for everyone, whether or not you use Forager.
- **Sign in to Forager with a passkey.** Your Forager account now supports passkeys, so you can sign in with your device using your fingerprint, face, or screen lock instead of typing a password or a code. Open your account's Security section to add a passkey, give it a name, and confirm the prompt from your device. On the sign-in page, tap "Sign in with a passkey" to get in the same way. A passkey cannot be guessed or phished, and there is nothing to type. It is an extra way in, not a replacement: your password and any two-factor sign-in keep working exactly as before, and you can remove a passkey from the Security section anytime.

## [0.15.15] - 2026-07-07
### Changed
- **A dedicated documentation site at docs.pantryraider.app.** The in-app help links now point to the published documentation instead of raw files on GitHub, and the docs are served from their own address.


## [0.15.14] - 2026-07-07
### Security
- **Restoring a backup now asks for your app password.** Restoring rewrites your settings and database, so the Restore panel now asks you to re-enter your current app password first, the same way downloading a backup does. Someone who walks up to an open Settings tab can no longer overwrite your setup. Installs with no password set are unaffected.

### Changed
- **Clearer, friendlier text in a few places.** The sign-in and Forager sign-in fields now announce themselves properly to screen readers, and recipe and item photos carry descriptions. The recipe-photo previews on the Recipes, Cooking, and Add screens now include alt text. The Import from a webpage box reads "Paste a recipe link and we'll pull in the ingredients and steps." The Settings menu and the Settings overview cards now use the same names for the Start Page & Stream Deck, Fleet & Remote Access, and Backups & Updates sections. The item-edit Category menu shows "(unchanged)" instead of a dash. The setup wizard's remote-access step now has a single clear Next button, and no longer mentions an internal config file.

## [0.15.13] - 2026-07-07
### Security
- **Camera preview cannot be pointed at this device or an internal address.** When you preview or show a camera, Pantry Raider fetches the picture for you on the server. It now checks the address first and turns down anything that points back at the app itself or at an internal-only address, so a camera address can only reach a real camera on your network. Your ordinary cameras, whether on the network directly, through Home Assistant, Frigate, or Reolink, are unaffected. The preview box for trying an address before you add it is also now limited to the admin sign-in, since adding a camera is a setup step.

## [0.15.12] - 2026-07-07
### Added
- **Preview a Reolink camera before adding it.** The Add a Reolink camera panel now has a Preview button, so you can see the picture and confirm the login works before you save the camera, just like the Frigate and network-scan camera flows. Enter the address and login, tap Preview, and the still image opens in a window. Your username and password are sent to the server and stay there: the app signs in to the camera for you and streams the picture back, so the login never appears in the web address or on the page. If the camera turns away the login or cannot be reached, you get the same clear message the Add button gives.

## [0.15.11] - 2026-07-07
- **Control the Ken Burns pan and zoom speed.** The photo screensaver's slow drift across each picture used to look nearly still on a small kitchen panel. There is now a Pan and zoom speed setting next to the Ken Burns option under Settings, Screensaver, with Slow, Normal, and Fast. Normal now moves enough to notice at a glance, and Fast gives each photo a bolder sweep. If your device is set to reduce motion, the slideshow holds each photo still and just crossfades.
### Security
- **Signing in to Forager with Google is safer.** When you link a kitchen by signing in with Google, Forager now only ever sends the one-time pairing code back to your own kitchen: an address on your own device, your home network, or your kitchen's own Forager web address. A tampered link that tried to send the code somewhere else is refused, so no one can trick a sign-in into handing your pairing code to another site.
- **Flagging a community recipe is one vote per member.** Reporting a shared recipe used to count every tap, so a single account could report the same recipe over and over to force it out of view. Now each member's flag counts once, repeat taps do nothing, and a recipe is only hidden when enough separate people have flagged it. Reports are also rate limited, the same as sharing.
- **The signup human-check no longer waves anyone through when it cannot be confirmed.** If the challenge that tells people apart from bots cannot be reached, signup now asks you to try again in a moment instead of letting the signup proceed unchecked.
### Fixed
- **The Stream Deck clock key follows your 12/24-hour setting.** The clock button on a physical Stream Deck always showed 24-hour time, even when the rest of your kitchen was set to 12-hour. It now reads the same clock format as your app pages and kiosk screens: 24-hour shows "15:42", 12-hour shows a compact "3:42P" that fits the key. This applies whether the deck is attached to your main server or to a satellite, and a deck that has not updated yet keeps its old 24-hour face.

## [0.15.10] - 2026-07-07
### Changed
- **Removed the keep-submenus-open navigation option.** It did not lay out cleanly and could overlap page content, so it has been taken out while the layout is reworked. The navigation menus behave as they did before.
- **Forager account settings and admin have a cleaner, roomier home.** The Forager portal now opens your account onto a landing page of category cards with a menu down the side, so your profile, security, plan and billing, kitchens, and recipe sharing each have their own tidy section instead of one long scroll. Your kitchens show their web address (with a copy button) and app version, and you can see the recipes you have shared and where each one stands. The operator admin gets the same side-menu and landing layout across the overview, accounts, recipe moderation, and stats.

## [0.15.9] - 2026-07-07
### Fixed
- **Reolink cameras now sign in with a token.** Newer Reolink firmware turns away the older way of putting your username and password straight into the picture request, so the camera came back saying the login was wrong even when it was right. Pantry Raider now signs in to the camera first, gets a short-lived pass, and uses that to fetch each still. The sign-in and the pass stay on the server and never reach your browser, and the pass is reused between refreshes so the camera is not asked to sign in on every frame. Adding a Reolink camera checks the real sign-in up front, so a wrong username or password is caught right away.

## [0.15.8] - 2026-07-07

## [0.15.7] - 2026-07-07
### Changed
- **With Forager, the AI model is chosen for you.** When Forager is your AI provider, the Scanning settings no longer show a model picker or provider key to fill in, because Forager handles the model for you. In their place you see a short note saying so, and the same applies to the barcode enrichment Model override: pick Forager there and it drops the model box rather than letting you type one that would be ignored. Choosing a direct provider (Gemini, OpenAI, Claude, or Ollama) still shows its own model and key fields exactly as before.

### Fixed
- **Imported recipes now really do arrive with their ingredients sorted out.** Reading an ingredient into its amount, unit, and food only took hold on some Mealie setups; on a current Mealie the recipe quietly saved as plain text instead. Now, when an AI provider is set up, saving a recipe (and the Parse ingredients button on a recipe you already have) creates or reuses the matching food and unit in your Mealie and links each ingredient to them, so the recipe lands genuinely parsed and the count you see is the number of lines that really came through. The ingredient text still goes only to the AI provider you chose, and the food and unit names only to your own Mealie. Anything that cannot be read is kept exactly as written, and if a save is ever refused it falls back to plain text so nothing is lost.

### Added
- **Add cameras from Frigate.** If you run a Frigate camera recorder, the Cameras settings can now list its cameras for you. Enter your Frigate address under Settings, Connections, and Find cameras: every camera it knows about shows up with a Preview so you can check the picture, and Add to drop it into your camera list in one tap. It works just like pulling cameras from Home Assistant, so your Frigate cameras appear on the Camera page and, with a Stream Deck attached, on the deck.

### Added
- **Reolink camera support.** You can now add a Reolink camera or video doorbell by its address and login. Its live picture and stills come through Pantry Raider, so the camera's username and password stay on your server and never show up in the page or on the camera address. Add one under Settings, Connections, Add a Reolink camera: fill in the address, the login, and (for a multi-camera unit) the channel, and it is checked and saved for you. Two-way talk on a doorbell is not part of this yet; this adds the reliable camera view.

## [0.15.6] - 2026-07-07
### Added
- **An Install app button when Pantry Raider can be added to your device.** On a phone or computer that supports installing web apps, a small Install prompt now appears so you can add Pantry Raider to your home screen or desktop for a full-screen shortcut, instead of hunting through the browser menu. On an iPhone it shows the quick Share then Add to Home Screen steps. Installing needs the secure (https) address, so open your kitchen through its public or tunnel link to add it.


### Added
- **A guided setup for hands-free barcode scanners.** A new wizard walks a scan-engine reader, like the Waveshare module, through its setup so it scans on sight without holding a button. It shows the reader's own setup codes one at a time, big enough to read off the screen, with a plain note on what each one does and Next and Back to move through them. Pick your reader from a short menu (the recommended hands-free setup is chosen for you), and where a step has a choice, the suggested code is shown first with the alternatives explained. Start it from a button on the Scanning settings, or scan the on-screen code to run it on your phone right next to the reader. It finishes with how to test that scanning works, and you can run it again any time.

### Added
- **Forager: your kitchen's web address on the dashboard.** When you turn on remote access for a kitchen, your Forager account page now shows that kitchen's web address, the link that reaches it from outside your home. Each kitchen lists its own full address as a link that opens in a new tab, with a Copy button so you can share it or save it for later. A kitchen without remote access set up says so plainly instead of showing a dead link.

### Added
- **The menu can keep a section's items in view while you are in it.** When you are on a page that lives under a section (say Convert or Nutrition under Kitchen Guide), the menu can now keep all of that section's items showing across the top, instead of hiding them back inside a drop-down. It is on by default in a browser and on small screens, where there is room for it; larger kitchen displays keep the tidy drop-downs. You can set it per screen under Settings, Screen, On-screen navigation: leave it on the default, always keep sections open, or always collapse them, and each device remembers its own choice.

## [0.15.5] - 2026-07-06

### Added
- **What can I cook now shows what you already own.** Open a recipe's ingredient list on the Cook page and every ingredient you already have is tagged: an "In stock" chip for things in your inventory and an "On hand" chip for things on your staples list, with only the rest making up a short shopping list. A link next to it drops straight into the staples settings so you can tell it what you always keep on hand (salt, oil, flour, and so on), and those items count as owned everywhere the Cook page checks. Adding a recipe's missing items to your shopping list now leaves out your staples too, so you are not buying what you already have.

## [0.15.4] - 2026-07-06

## [0.15.3] - 2026-07-06
### Added
- **Imported recipes arrive with their ingredients already sorted out.** When you have an AI provider set up, saving a recipe into Mealie now reads each ingredient line into its amount, unit, and food as it saves, so the recipe lands ready to use instead of showing Mealie's "these ingredients aren't parsed yet" prompt. This covers every way a recipe gets in: a web link, a PDF, a photo, a file, a community recipe, or one you typed yourself. The ingredient text goes to the AI provider you chose, the same as the rest of the app's AI features. If a line cannot be read, it is kept exactly as you wrote it, so nothing is ever dropped, and if AI is not set up recipes save the way they always have.
- **Parse ingredients for a recipe you already have.** Open one of your saved recipes in its quick view and, with an AI provider set up, you will see a Parse ingredients button. It reads the recipe's ingredient lines into tidy amounts, units, and foods and saves them back, so an older recipe that imported as plain text gets cleaned up in place without opening Mealie. It shows its progress and tells you how many ingredients it sorted out, and it leaves any line it cannot read untouched.

## [0.15.2] - 2026-07-06

### Changed
- **Your device password and Sign in with Forager are now clearly separate.** The sign-in screen spells out that your device password always works, even if Forager is offline, and that Sign in with Forager is an added convenience using your Forager account. The device password never depends on the Forager server being reachable, so a cloud or internet outage cannot lock you out. Settings now reminds you to keep a device password set and remembered as your fallback whenever this device is connected to Forager, and warns you to set one first if you have connected Forager without a device password in place.
- **Settings reads cleaner: updates first, and the technical bits tuck away.** In Updates & Backups, keeping the app current is now the first thing you see, with backing up your data right below it. The command line steps for a manual update stay a click away behind a "Command line update" link, so the everyday buttons are not buried under commands. Under Advanced, the satellite switch, maintenance actions, and diagnostics now sit behind short "Show" links too, so the page opens tidy and you expand only what you need.

### Fixed
- **The "Connect Forager" button on the Recipes settings now takes you there.** When your kitchen is not yet linked to Forager, the Recipes settings offer a Connect Forager button; it now opens the Forager settings so you can sign in, instead of doing nothing.
### Added
- **Tidy up a recipe before you save it.** The recipe review editor has a new Optimize button that hands your draft to your AI provider for a cleanup pass: clearer wording, steps put in order and split apart where one line crammed in several actions, consistent units, and timing cues written so the app's kitchen timers can pick them up (for example "simmer for 20 minutes"). It keeps the same ingredients, amounts, and method, so it only changes how the recipe reads, not what it makes. The result drops back into the editor for you to look over and save, and nothing is saved until you do. Optimize shows up once an AI provider is set up.
- **Open in Mealie from On the Line.** When the recipe you are cooking came from Mealie, the On the Line page now shows an "Open in Mealie" link next to its title, so you can jump straight to the full recipe in Mealie for the details you keep there. It only appears for Mealie recipes and when Mealie is set up.

### Fixed
- **Recipe PDFs with fancy fonts import cleanly.** Some PDFs store their text with decorative fonts that used to import as scrambled characters (a title like "Dan's Kitchen Chicken Recipes" could come through as gibberish). The app now cleans up that text before reading the recipe: it fixes joined letters like "fi" and "fl", strips out unreadable symbols, and when a PDF's text is mostly garbled it tells you so and suggests taking a photo instead, rather than drafting a nonsense recipe.
### Added
- **Forager: upload your own recipes from the portal.** Signed in on the Forager website, you can now add your own recipes to the community from the new Share a recipe page: type it in, upload a PDF, or upload a photo. Forager reads it and tidies up the formatting for you, keeping your ingredients, amounts, and method exactly as written, then shows you a draft to check and edit before anything is shared. A PDF that is just a scan with no readable text points you to the photo option instead. Uploading is for kitchens actively using Pantry Raider, so connect a kitchen (or ask us to turn it on for your account) and every shared recipe carries a credit line saying who to thank.

## [0.15.1] - 2026-07-06

### Added
- **Read a recipe in the app without opening Mealie.** Your own saved recipes on the Recipes page now open a quick view right where you are: tap a recipe's name or its new View button to read the full ingredients and steps in a pop-up, no second tab needed. From there you can start cooking it (it becomes your Current Recipe) or open it in Mealie if you want the full editor. Web and community recipes keep their existing preview with Save and Cook.

## [0.15.0] - 2026-07-06

## [0.14.3] - 2026-07-06

### Added
- **Our Picks: the hardware we use to build a Pantry Raider display.** The Shop page now leads with a curated "Our Picks" section covering the parts for a real build: a Raspberry Pi to run it, a small touchscreen to see it, an Elgato Stream Deck for one-touch controls, a barcode scanner for fast pantry entry, and the power supply, memory card, and case that hold it together. Each pick has an honest one-line description and a "why we like it" note, grouped by category, with a "View on Amazon" button. These are affiliate links, so buying through them supports the project at no extra cost to you, and the section says so up front.
### Changed
- **Forager now versions its database schema with Alembic.** The hosted service manages its Postgres schema through numbered migrations instead of only ever adding tables at startup, so future changes to accounts, subscriptions, community recipes, and the rest can be rolled out cleanly and reviewed before they run. The change is infrastructure only: no table or column changed, existing Forager accounts and data are untouched, and the live database is adopted into the new system with a one-time step that alters nothing.
### Added
- **Import a recipe straight from a PDF.** The Recipes page has a new "From PDF" button next to From Photo and From File: pick a recipe PDF and the app reads its text and drafts the recipe for you, with the name, ingredients, and steps filled in for you to check over and save, the same review step as importing from a webpage. The PDF's text is sent to the AI provider you set up, the same way a photo import sends the picture. If the PDF is a scan or images with no readable text, it tells you so and suggests taking a photo instead; oversized files and non-PDFs get a clear message rather than a silent failure. From PDF appears once an AI provider is set up.

### Fixed
- **Importing a recipe from a link works on more sites.** When Mealie's own importer cannot read a page, the app now fetches it the way a normal web browser does, so ordinary recipe sites stop turning the request away. If a site still cannot be read, the message tells you what to do next: a missing page suggests checking the link points to a single recipe rather than a list, a site that blocks the request suggests copying the recipe text in instead, and a site that will not respond points at the link or your connection, instead of showing a raw error.

## [0.14.2] - 2026-07-06

### Added
- **Add a set of community recipes to your library at once.** Once you are connected to a Forager account, the Recipes settings pane has a new "Start with a set of community recipes" action that copies a batch of community recipes straight into your recipe library in one click, so a fresh kitchen has plenty to cook right away instead of saving recipes one at a time. It skips any recipe you already have, so it is safe to run again, and it tells you what happened ("Added 23 recipes, skipped 4 already in your library"). You need Mealie set up as your recipe library to use it; if a recipe or two cannot be copied this time, the rest still come through and the count says so.

## [0.14.1] - 2026-07-06

### Added
- **Recipe cards now show where each recipe came from.** Every recipe in the browser and in What Can I Cook? wears a small source chip, so at a glance you can tell your own recipes from ones you imported from the web, one-off web results, and recipes shared through the Forager community. Your own Mealie recipes read as "My recipes", a recipe you imported from a webpage reads as "Mealie (imported)", TheMealDB and Spoonacular results read as "Web", and community recipes read as "Forager cloud". The chips are quiet, colour-coded, and stay out of the way of the buttons.
- **See how many times you have made a recipe.** Once you cook a recipe, its card picks up a "Made N times" note (with the last time you made it on hover), so a tried-and-true dinner stands out from something you have never cooked. Every way of cooking counts it: the Cook button on the Recipes page, "Mark cooked" on the Current Recipe page, and cooking a course. Recipes you have never made simply show nothing.

## [0.14.0] - 2026-07-06

### Added
- **Browse and share Forager community recipes in the app.** Once your install is connected to a Forager account, the Recipes page shows recipes shared by other Pantry Raider kitchens right alongside your own and the web sources: turn on "Other recipes" to search and browse the community library, preview any recipe, and save the ones you like into your recipe collection with their credit line intact. A new Share button on your own recipes posts them to the community; you fill in who to credit, and that is required so sources always travel with the recipe. There is a matching on/off switch under Settings, Kitchen, Recipes ("Show community recipes"), on by default once you are connected. If Forager is ever unreachable, community recipes simply do not appear and the rest of the Recipes page keeps working.

## [0.13.4] - 2026-07-06

### Added
- **Forager gets a moderation panel for community recipes.** The Forager admin now has a Recipes page that lists every shared recipe with its title, who submitted it, its credit line, average rating, report count, and status. You can filter by pending, approved, hidden, rejected, or a Reported view that surfaces anything members have flagged along with the reasons they gave, so trouble is easy to find and act on. Each recipe has one-click actions to approve, hide, restore, reject, or delete it, and hiding, rejecting, or deleting a recipe pulls it from the public browser right away. A new setting controls how new submissions arrive: by default a shared recipe is approved and visible the moment it is posted (reactive moderation, with member flags auto-hiding trouble), and a moderator can switch on require-approval so recipes wait for a review before anyone sees them. The main admin overview now shows a quick tally of pending, approved, hidden, and reported recipes that links straight into the moderation page.

## [0.13.3] - 2026-07-06

### Added
- **Forager community recipes: share your recipes and browse everyone else's.** Signed-in Forager members can now post a recipe to a shared community library and save copies of recipes other people have shared. Every shared recipe carries a credit line you fill in (who to thank or where it came from), so sources always travel with the recipe. You can search the library by title or ingredient, rate a recipe from one to five stars (rating again updates your rating rather than piling on a second one), and flag anything that does not belong; enough flags quietly pull a recipe from the browser until it can be looked at. Sharing and saving are free for every member, with no plan required. This first step is the shared library itself; a review workflow and in-app browsing arrive next.

## [0.13.2] - 2026-07-06

### Added
- **Changing your login password now asks you to confirm it.** The Change password box in Security has a second field, Confirm new password, next to the new one. If the two do not match, the save is stopped with a clear note ("The new passwords do not match") instead of quietly saving a typo that could lock you out. Leaving the field blank to keep your current password works exactly as before, and you still enter your current password to change one that is already set.
### Changed
- **Forager: the free trial is now one per install.** The 30-day trial starts the moment you create your Forager account, and each copy of Pantry Raider now gets that free trial only once. When you create an account from the app's sign-in link, the install quietly tells Forager which install it is (an opaque per-install id, not personal information), so someone cannot keep making new accounts on the same install to farm one free month after another. If you create a second account from an install that already used its trial, that account still works, it just starts without a trial and can subscribe to keep going ("This device already used its free trial. You can subscribe to keep going."). Signing up from a plain web browser, and every existing account, trial, comp, and paid subscription, is unaffected.

## [0.13.1] - 2026-07-06

### Fixed
- **On-screen Start Page keys now show live content like the Stream Deck.** The full-screen Start Page used to draw its weather, forecast, expiring, and camera keys as plain icons while the physical Stream Deck showed the real thing. Those keys are now live: the weather key shows the current temperature and conditions, the forecast key shows today's high and low, the expiring key shows a prominent count of items expiring soon (and stays calm when nothing is), and the camera key shows a live snapshot preview behind its label. They refresh on their own gentle schedules, pause while the page is in the background, and quietly fall back to the plain icon if the data cannot be reached, so a key is never broken. Timers keep working exactly as before.

## [0.13.0] - 2026-07-06

### Added
- **The AI can now estimate how long an item keeps and where to store it.** Scanning & AI has a new option, under Barcode enrichment, called "Estimate shelf life and storage." When it is on and you already have an AI provider set up, adding an item by barcode, food photo, or receipt also asks the AI for a realistic best-by window and the right place to keep it (refrigerator, freezer, or pantry), and uses that instead of the generic category rule. That fixes items the built-in rules get wrong, like a refrigerated cheesecake that would otherwise come in as a room-temperature pantry item. A printed date on the packaging still wins, and if the AI cannot answer the item falls back to the usual category default, so nothing breaks. The option is off by default and only appears once a provider is configured. As with all scanning, the item, and any photo or receipt image, goes to the AI provider you chose.

### Changed
- **Recipe tastes moved out of the Mealie connection settings.** The Recipes page used to hold two very different things: your Mealie connection and recipe sources, and your personal suggestion tuning (pantry staples, taste preferences, kitchen appliances, and the ranking numbers). Adjusting your tastes sat right next to the Mealie address and API key, so it was easy to worry a save there might touch your connection. Your suggestion tuning now lives on its own Recipe suggestions page under Kitchen, saved on its own, so changing your tastes can never overwrite the Mealie connection, and setting up Mealie never resets your tastes. Nothing about how suggestions work changed, only where you set them.
- **Turning on kiosk mode now warns you that it hides the mouse cursor.** The tablet button in the top bar switches this screen into kiosk (touch) mode, which sizes everything for a fingertip and hides the mouse pointer. Flipping it on from a normal browser used to make the cursor vanish with no explanation. Now a quick prompt spells it out before it applies and reminds you that clicking the same tablet button again turns kiosk mode off and brings the cursor back.
- **A new Pantry Raider theme, and it is now the default.** Appearance has a "Pantry Raider (brand)" theme that dresses the whole app in the look of the pantryraider.app site: a calm graphite dark background with the raccoon pink used for buttons, links, and the highlight around whatever you are typing in. New installs start on it. If you already picked a theme, your choice is kept exactly as it was; nothing changes until you switch to it yourself from Settings, Appearance.

### Fixed
- **Cyborg theme text boxes are readable again.** On the Cyborg theme, text you typed into search and entry fields, and the grey hint text inside them, was washed out and hard to read against the light field. Those fields now show dark, clearly legible text with a readable placeholder. The rest of the Cyborg look is unchanged.

## [0.12.0] - 2026-07-06

### Added
- **A new Status page shows everything's health at a glance.** Settings has a Status page, right below Overview, that gathers the health of this device and the services it talks to into one dashboard. Each row shows a colored badge, green for healthy, amber for something that needs a look, red for a problem, with a short line explaining it and a Manage link straight to the setting that fixes it. It covers this device's connection (and, on a kitchen appliance, its power, temperature, and storage), whether you are on the latest version, your Forager account and remote access, and your Grocy, Mealie, and Home Assistant connections. Only the rows that apply to your device show, and a Refresh button re-checks on demand.
- **Settings opens on a new Overview.** Settings now greets you with a map of everything you can set up: a grid of cards grouped under Kitchen, This Device, Connections, and System, each with a plain-language line about what it covers. Pick a card to jump straight to that section, or use the menu as before. On a satellite display the cards for things it does not manage (like your inventory and your cloud account) are left out, so you only see what applies to that device.

### Changed
- **Security settings are clearer and tidier.** The Security page was rebuilt so each control sits on its own compact row with a short label and an at-a-glance state (On/Off, Set, Required). Your login password now hides behind a Change button instead of showing paired boxes, so you only enter your current password when you actually want to change it. API keys are laid out as a simple table (name, key, type) with Reveal, Regenerate, and Create key actions, making it obvious which key is the primary one and which belong to your satellite displays. The reverse-proxy and two-factor explanations moved into hover or tap info tips so the page reads quickly.
- **The Network page is denser and easier to scan.** Wi-Fi, Ethernet, and the two device-name fields now sit in tight labelled rows under clear panels (Connection, Device names, Attached hardware) instead of tall cards, so the whole page fits on screen and each setting is easy to find. What each control does is unchanged.
- **The rest of Settings now uses the same tidy layout.** Every remaining Settings page (Inventory, Scanning & AI, Recipes, Appearance, Display & Sleep, Start Page, Cameras, Home Assistant, Fleet & Remote Access, Forager, Backups & Updates, and Advanced) was rebuilt to match Security and Network: each setting sits on its own compact row with a bold label, a one-line hint, and an at-a-glance state chip (like Connected, Set, or Off), with the longer explanations tucked into hover or tap info tips. Test buttons, editors (the Stream Deck and Start Page grids, storage categories, the navigation editor), QR codes, and everything else work exactly as before, just in a layout that fits on screen and reads quickly. The first-run setup wizard got matching touch-ups too.

## [0.11.0] - 2026-07-06

### Added
- **Wi-Fi has its own Network page.** Wi-Fi used to be buried at the bottom of the old Devices & Fleet page, where it was hard to find. It now has a dedicated Network page under the new This Device group, leading with your Wi-Fi connection and the option to switch networks, followed by the device's name on the network and the attached hardware it can see. Everything about how this device connects is in one obvious place.
- **Home Assistant has its own page.** The Home Assistant connection and its on-screen notification and camera pop-up settings moved out of the crowded Connections page onto their own Home Assistant page under Connections, so the connection details and the token are easy to find and change.

### Changed
- **The Settings menu is reorganized to be easier to scan.** Settings are now grouped under four plain-language headings, Kitchen, This Device, Connections, and System, in a single menu, so every page is one click away and nothing is hidden behind a toggle. Recipes (Mealie and the recipe sources) sit together under Kitchen, cameras and Home Assistant under Connections, and the fleet and remote-access tools under System. Long inline explanations across the busiest pages were trimmed into small info icons you can hover or tap, so pages read at a glance. Your existing settings are unchanged; only where they appear moved.

### Fixed
- **The two hostname fields are no longer confusing.** The Network page used to show two similarly named hostname boxes with no clear difference. They now sit together with distinct labels: "This device's name on the network" actually renames the device (so it is reachable at that name.local), while "Name used in on-screen links" only shapes the links the app shows you and never renames anything. Each has a short note and an info icon spelling out what it does.

## [0.10.1] - 2026-07-06

### Fixed
- **The Mealie card knows when Mealie is already running.** On a Pi appliance, the Recipe Manager card in Settings kept offering "Start Mealie on this device" even after Mealie was up and serving. It now checks with the device first: when Mealie is already running it shows a clear "Mealie is running on this device" indicator with a link to open it, and only offers the start button when Mealie is stopped or not yet installed.
- **Wi-Fi no longer looks broken when you are on Ethernet.** A Pi connected by a network cable showed its Wi-Fi status as "unavailable", which read like an error even though the device was online. The Wi-Fi line now recognises when Ethernet is carrying the network and shows a calm "Connected by Ethernet, Wi-Fi is available but not in use" instead. A genuine Wi-Fi problem, when Wi-Fi is the connection you are relying on, still shows up as before.

## [0.10.0] - 2026-07-06

### Added
- **Install Pantry Raider like an app.** Pantry Raider is now a progressive web app, so your phone, tablet, or computer can add it to the home screen or dock and open it in its own window with the pink raccoon as its icon, no app store needed. Look for "Install" or "Add to Home Screen" in your browser's menu. The installed app opens straight to your kitchen and always loads live data when it can reach the server; only the look-and-feel files are kept on the device so it opens fast. Installing needs a secure (https) address, such as a Forager remote-access web address, so a plain home-network address will still open in the browser as before.
### Security
- **Downloading a backup now asks for your password.** A settings backup can carry your API keys and passwords, and until now anyone standing at an already-open Settings page could download the whole thing. The Download Backup button now asks you to confirm your current password first, and the file is only sent once it matches. Nothing changes on an install with no login password set, and a wrong password just shows a short prompt to re-enter it. The one-click support bundle is unaffected: it already blanks every secret before it leaves the device, so it needs no password.
- **A read-only demo mode for public instances.** A Pantry Raider install can now be put in demo mode, where anyone can open it and explore every page, the pantry, recipes, timers, settings, all of it, but nothing they do changes anything. A slim pink banner across the top makes it clear you are in a live demo, and any attempt to add, consume, scan, or save a setting is politely turned away instead of taking effect. It is off unless the host turns it on, so a normal kitchen is never affected, and it cannot be switched off from inside the demo itself.

## [0.9.0] - 2026-07-06

### Added
- **Forager: spam protection on signup with Cloudflare Turnstile.** When configured, the signup page shows a Cloudflare Turnstile challenge (usually invisible) and the server verifies it before creating an account, on top of the existing honeypot, rate limit, and disposable-email checks. A Cloudflare outage never blocks a real signup.
- **Two-factor authentication for this device.** Settings, Security now has a Two-factor authentication (this device) section: turn it on with any authenticator app (scan the QR or type the key), confirm a code, and save the ten one-time recovery codes shown once. After that, signing in asks for a 6-digit code (or a recovery code) after your password. Turning it off, or making fresh recovery codes, needs a current code or your password first, so no one at an open settings page can quietly remove it. It works offline, with no email or text message.
- **Sign in to the app with your Forager account.** Once a device is connected to Forager, the login screen offers a second way in alongside the device password: Sign in with Forager, using the same email and password as your Forager account. If your account has two-factor turned on, it asks for the code too. Handy when several people share one kitchen and each already has a Forager account, and it means you do not have to hand out the device password.
- **Forager: two-factor authentication.** Accounts can turn on 2FA with any authenticator app (scan a QR or enter the key), backed by ten one-time recovery codes. After that, sign-in asks for a 6-digit code, a reset or an abandoned code entry never signs anyone in, and turning it off needs a current code, a recovery code, or the account password.
- **Forager: forgotten-password reset and email verification.** The Forager portal can now email a password-reset link from the login page and send a verification email at signup, once an email sender is configured. A reset logs out other sessions; verification is advisory, so signups never fail if email is briefly unavailable.
- **The screensaver shows iPhone photos.** Photos taken on an iPhone are saved in Apple's HEIC format, which web browsers cannot display, so they used to be skipped in the screensaver slideshow. Drop them on a USB drive as-is and Pantry Raider now converts each one to a standard JPEG on the fly, so your iPhone pictures play in the slideshow right alongside everything else. Nothing to convert or rename first.
- **Start from a hardware preset.** Setting up a known build no longer means filling in each hardware setting by hand. The wizard's Hardware step and the Screen & Sleep page now have a Start from a preset picker: choose your kit and one click fills in the display rotation, screen size, display type, and Stream Deck (size and rotation) for that hardware. The first preset is The Bandit v1.0, a 7-inch touch panel mounted in portrait with a 15-key Stream Deck turned on its side. You can still adjust anything afterward, and a preset only touches the hardware settings it names, so nothing else you have set up is disturbed.
- **Photo screensaver options: timing and Ken Burns.** The photo slideshow settings under Screen & Sleep now let you set how many seconds each photo stays up and turn the Ken Burns pan and zoom on or off, so you can hold each picture still or keep the classic slideshow motion.
- **Forager has its own place in Settings.** Everything about your Forager account now lives together in a new Forager page in the Settings menu: sign in, see your plan and how much of your monthly allowance is used, turn on remote access, and disconnect, all in one spot instead of tucked inside AI & Scanning. The AI & Scanning page stays focused on your own scanner and AI providers, and points you to the Forager page when scanning is running through Forager. The Forager page appears on a main install; a Pi Remote uses the main server's account, so it does not show one of its own.
- **Reach your kitchen from anywhere with Forager remote access.** On a Pi appliance connected to Forager, the Forager page in Settings now has a Remote access switch. Turn it on and your kitchen gets its own web address that works away from home, not just on your home network, with no router settings or port forwarding to figure out. It is included with a Forager plan. Remote access asks you to set a login password first, so your kitchen stays private once it is reachable from the internet, and once it is on the app uses that same address for phone QR codes and outward links so everything works the same at home and away. Turning it back off returns the kitchen to your home network only.
- **Forager pricing: a 30 day free trial, then Cloud Basic or Premium.** Pantry Raider stays free and open source with your own AI key; Forager is the optional convenience plan for remote access and AI scanning credits. Every account starts with a 30 day trial at the full Premium allowance. After that, Cloud Basic ($10 a year) keeps remote access with a small AI allowance for people who bring their own key, and Premium ($3 a month or $30 a year) adds the full AI allowance. The app and the wizard show your plan and, on a trial, the days remaining.
- **Screensaver timer pills come in three sizes.** A new Timer pill size choice (Normal, Large, Extra large) under Screen & Sleep makes the floating countdowns readable from across the kitchen on smaller panels.
- **Pick a 12-hour or 24-hour clock.** Settings, Advanced, Date & time now has a Clock format choice next to the timezone: Auto keeps the familiar reading each surface has always used, 12-hour shows 3:42 with a small PM, and 24-hour shows 15:42. It applies everywhere a time of day appears: the screensaver clock, the weather page's hourly strip and sunrise and sunset, and timestamps like the last update check. Like the timezone, it is set once on the main server and every Pi Remote follows along, so all the clocks in one kitchen agree.
- **A small way to say thanks.** The About & Credits page now has a Support the project button alongside the donation links for Grocy, Mealie, and the other projects Pantry Raider is built on. Pantry Raider stays free for home use; the button simply buys the developer a coffee if the app has earned it.
- **A second password for the rest of the household.** Settings, Security now has an optional Viewer Password next to the main UI password. Anyone who logs in with it can use the whole kitchen: browse the pantry, run timers, scan barcodes, and cook from recipes, while Settings, backups, and updates stay behind the main password. Leave it blank and nothing changes; only the main password logs in.
- **Sign in to Forager right from the app.** The Forager page in Settings starts with a simple sign-in: the email and password from your Forager account, an optional kitchen name, and one button. Signing in connects the device to your account and, on a fresh install, switches scanning and barcode enrichment over to Forager automatically, so photos, receipts, and barcodes just work with nothing else to set up. If you already had your own AI provider working, it stays in charge and the card offers a one-tap Use Forager for scanning switch instead. When Forager offers it, a Continue with Google button signs in without typing a password at all. Your password goes to Forager only, to sign you in, and is never saved on the device. The first-run wizard leads with the same sign-in on its AI step, the pairing-code path moved under an Advanced toggle for those who prefer the website, and Disconnect now also signs the device out on the Forager side. When your Forager plan includes a web address for the kitchen, the app adopts it for phone QR codes and outward links so the address works the same at home and away.
- **Link your kitchen to Forager.** The Forager page in Settings lets you type in a pairing code from the cloud portal and this install links to your cloud account. Once linked, pick Forager as the AI provider and photo analysis, receipt parsing, and barcode enrichment run through your subscription, no API key of your own needed. The card shows the account's plan and how much of the monthly AI quota is used, right next to the local token counter, and an Unlink button forgets the link whenever you want. Every install pairs separately, satellites included, so each device shows up by name on your account. If the monthly quota runs out, AI features pause with a clear message until the new month, exactly like the local token budget; everything else keeps working.
- **Turn on remote access while you set up.** The first-run wizard's Remote Access step now offers both ways to reach your kitchen from away, not just Cloudflare. Forager, included with a plan, gives your kitchen a ready-made web address and can be switched on right there once you are signed in to Forager and have set a login password; if either is still missing, the step tells you which one to finish first instead of hiding the option. Cloudflare Tunnel stays the free do-it-yourself choice you complete in Settings after the wizard.
- **A quick way to create a Forager account.** The Forager sign-in, in the wizard and on the Forager page in Settings, now has a Create an account link that opens the Forager signup page in a new tab, so anyone new can make an account and come straight back to sign in. The kitchen name field also notes that it becomes part of your kitchen's web address and is tidied to letters, numbers, and dashes as you type, so a friendly name like "Home Kitchen" just works.
- **Pick your kitchen's web address.** When you turn on Forager remote access, you can now choose the web address people use to reach your kitchen instead of taking whatever your device happens to be named. The Remote access card has a Web address field with your kitchen's own name in front of the shared Forager address, and it checks as you type whether the address is free, offering a nearby one when your first pick is taken. Leave it blank and it still uses your device name, just as before. To change the address later, turn remote access off and back on with the new one.
### Changed
- **Signing in from outside your home network needs a second factor.** When your kitchen is reachable from the internet through Forager remote access, a login that comes in over that web address must pass a second step: the device password now also needs a two-factor code, or you sign in with your Forager account (which carries its own two-factor). On your home network and on the kitchen display, sign-in is unchanged. If a device is exposed to the internet without device two-factor set up, the login says so and points you to sign in with Forager or to set up two-factor from home first.
- **Remote access asks for a second factor before it goes live.** Turning on Forager remote access now checks that a second factor is in place first: either two-factor for this device (Settings, Security) or a Forager account that has two-factor. If neither is set up, the switch explains what to turn on, so your kitchen is never put on the internet behind a single password. The existing checks (connected to Forager, login password set) still apply.
- **The setup install log is readable during image pulls.** On a first-boot Pi install the setup screen streamed Docker's raw progress, which floods with a new line for every byte of every layer and made a normal, one-time pull look stuck. It now shows the real messages plus a short live summary per image (how many layers are done and whether it is downloading or extracting), with a note that pulls are slower on an SD card.
- **Forager remote access now works on a server, not just a Pi.** Turning on Forager remote access used to be limited to a Pi appliance; a regular server install turned it down. Now a server can host its own secure tunnel too, so any Pantry Raider gets a web address that works away from home. The tunnel runs inside the app itself, so nothing extra to install. The stock Docker Compose files already grant what it needs; on Unraid, add `--cap-add=NET_ADMIN --device=/dev/net/tun` under Extra Parameters and the `net.ipv4.conf.all.src_valid_mark=1` sysctl. If a server cannot support it, the switch says so plainly instead of failing quietly.
- **One clear set of remote-access choices.** Remote access is now a single section on the Forager page with three modes: Off, Cloudflare Tunnel, and Forager. Cloudflare Tunnel is the free, do-it-yourself option where you run a small helper yourself; Forager gives your kitchen a ready-made web address that is included with a plan. This replaces the two separate remote-access controls from before (an older card under Connections and the newer Forager switch), so there is one place to pick how you reach your kitchen from away. Any choice you had set is carried over.
- **The Stream Deck now rests with the display.** When the kiosk display goes to sleep, an attached Stream Deck shows the Pantry Raider raccoon across its keys instead of the normal buttons, right way up whatever way the deck is turned, and brings the buttons back the moment the display wakes. This replaces the screensaver-spanning option from 0.8.0 (where the bouncing logo glided across the deck keys); the Screensaver position setting is gone, and a new Logo while display sleeps switch in the Stream Deck settings controls the resting face, on by default. The deck's own idle timeout is unchanged and still blanks the keys fully after its own quiet period.

### Fixed
- **Forager sign-in asks for your two-factor code.** If your Forager account has two-factor sign-in turned on, connecting a device with your email and password now prompts for the code from your authenticator app (a recovery code works too) and finishes signing in once it matches, instead of stopping with a confusing "email and password did not match" message. The code field appears only when your account needs it, and a wrong code says so plainly.
- **Continue with Google honors two-factor sign-in when connecting a device.** Signing in to Forager with Google from the app now asks for your two-factor code before the device is connected, the same as signing in on the website, so an account with two-factor turned on is protected on every way in. Accounts without two-factor connect in one step exactly as before.
- **Updates recover from a stuck container recreate.** A Pi Hosted update could fail with a container name conflict when a previous interrupted update left a renamed container holding the name, leaving the message "could not recreate the service container". The update now detects that conflict, removes the leftover container, and retries automatically.
- **The Copy button for your kitchen's web address works again.** On the Forager Remote access card, the small Copy link next to your kitchen's web address did nothing on the kitchen display and on home-network browsers, where the usual copy shortcut is blocked. It now falls back to the same reliable copy the rest of Settings uses, so one tap puts the address on the clipboard and briefly shows "copied".
- **Scanned receipts now wait for you in Pending.** Snapping a photo of a receipt read all its items, but they only sat in a list on the add page and were gone the moment you navigated away without importing. Receipt items now go straight to the Pending list the instant they are read, so they stay put until you review, edit, and add them to your pantry, the same way a barcode scan does.
- **Changing the app password now asks for the current one.** Setting or removing the UI login password on a device that already has one requires entering the current password first, so an open or unattended settings page cannot be used to change it and lock you out. Setting the first password is unchanged.
- **Starting Mealie no longer leaves the setup step spinning.** When you started Mealie from the setup wizard, the Start button could keep spinning with no way forward if a progress check went unanswered, and the only fix was to reload the page. Every progress check now gives up quickly instead of waiting forever, so the Mealie step always settles on its own, whether Mealie comes up, takes too long, or runs into an error.
- **Screensaver photos work from a plain USB drive on a Pi.** The photo slideshow could not find a drive on a Pi appliance that had no automounter, and it ignored drives mounted read-only. The device helper now reads photos from an already-mounted drive (read-only included) and, when nothing has mounted the drive, mounts it read-only itself, so a stick with a photos folder just works after plugging it in.
- **Tighter browser security out of the box.** The app no longer tells browsers that any website may call it from another origin. Every real client (the web UI, the kiosk, Home Assistant, satellites, the Stream Deck) is unaffected because none of them ever needed that allowance; it only made it easier for an unrelated web page to probe the app on your network.
- **The cloud backup remote is checked before use.** The rclone remote in Settings, Backup now has to look like a real destination (`remote:path` or an absolute path). A malformed value is rejected when saved instead of being handed to the backup command as-is, so a typo (or something sneakier) can never turn into an unexpected command option.
- **Waking either surface now wakes both.** A Stream Deck press has always woken a sleeping display, but touching the screen could leave a blanked deck dark until its own next press. The deck now picks up screen activity reliably, even when its check runs late, so a tap on the panel relights the keys within moments. The two idle timeouts stay independent; only the wake is shared.
- **Floating timer chips on every page.** While kitchen timers are running, each one now shows as a small countdown chip in the bottom corner of whatever page you are on, so a timer started from the Stream Deck or the Timers page stays in sight while you browse the pantry or a recipe. Tap a chip to jump to the Timers page. A new Floating timer chips choice sits with the other display settings: Auto (the default) shows them everywhere except at large and extra-large interface scale, where a small kiosk screen has no room to spare, and Always show or Always hide override that per install.
- **The appliance warns you about hardware trouble.** A Pi appliance now checks itself every minute for the classic Raspberry Pi problems: not enough power (a weak supply or a charge-only USB cable), running hot, CPU throttling, and storage filling up. An active problem shows up in three places: the warning icon in the top bar, a banner on the Settings device page, and a notification in the inbox that explains the likely fix in plain terms, with the power ones linking straight to the Power and cabling guide. Each warning appears once, stays while the condition holds, and clears itself from the inbox when the condition goes away.

## [0.8.0] - 2026-07-04

### Added
- **The device helper now requires an on-device token.** On a Pi appliance, the helper service that performs updates, reboots, and restores now hands the app and the Stream Deck a private token when the device starts, and expects it back on every request that changes anything. It all happens on the device itself, so nothing changes in day-to-day use and no action is needed; this release still accepts requests from software that predates the token, so mixed-version devices update cleanly.
- **One-click support bundle.** The Diagnostics card in Settings, Advanced now has a Download support bundle button that packs everything useful for a bug report into a single zip: the app version, your settings, the captured log, the current scanner and pantry-audit state, running timers, the last update check, and the Python environment. On a Pi appliance the bundle also includes a device health report gathered on the host: service states, boot settings, display and input probes, disk space, power throttling, and the most recent update log. Attach the zip when reporting a problem; passwords and API keys are removed from every file in it, so it is safe to share.
- **Choose an update channel.** Settings, Backup & Updates now has an Update channel choice: Releases only updates to tested, numbered releases, while Latest installs every change as soon as it is published. The choice covers the whole fleet, so satellites follow their main server, and Check for updates compares against whichever channel is picked. Latest remains the default for now so existing devices keep updating exactly as before; from release 0.8.0 on, Releases only is the recommended setting for everyday kitchens.
- **A finished timer crosses onto the Stream Deck.** With the screensaver spanning the Stream Deck, a timer that has gone off no longer stays confined to the screen: its pulsing Done pill drifts off the panel edge and across the deck keys just like the gliding raccoon, flashing red and amber with its food icon so an expired timer catches the eye on both surfaces. Running timers keep to the screen; only a finished one earns the extra territory. Pressing any deck key still wakes both surfaces at once.
- **Clear all timers in one tap.** The Timers page has a Clear all button next to the presets whenever anything is on the clock: it stops every running and finished timer at once, on the Stream Deck and every other screen too, after a confirmation that names how many timers it is about to clear. Handy when dinner is served and three countdowns no longer matter.
- **Start the screensaver from the Timers page.** A small Screensaver button in the Timers page header sends the display straight to the screensaver, no waiting for the idle timeout (it works even with the screensaver set to off). Set your timers going, tap it, and the screen dims to the saver with the countdown pills floating across it until you touch the screen again.
- **Approximate cost next to AI token usage.** The AI token usage card in Settings now shows a rough dollar figure next to this month's and all-time token counts, priced from the published list prices of the model you have selected. The tracker counts input and output tokens together, so the figure uses a blended rate and is an estimate only; list prices also change over time, so your provider's bill is always the real number. A custom model the app does not recognize shows tokens only, with no guess.
- **Weekly scheduled reboot.** The automatic kiosk reboot in Settings, Personalization, Screen & Sleep now has a frequency choice: Off, Nightly, or Weekly with a day-of-week picker, so an appliance that only needs an occasional refresh can reboot once a week instead of every night. Existing installs are untouched: a reboot time saved before this change keeps rebooting nightly until the frequency is changed.
- **An on-screen keyboard for kiosk touchscreens.** Tapping a text field on a kiosk screen now slides a touch keyboard up from the bottom of the page, so a wall-mounted panel can name a custom timer, type a barcode, add to the shopping list, search, and fill in settings without a physical keyboard. It has big touch-friendly keys with shift (tap twice for caps), a digits row with symbols, and Enter that submits just like a real keyboard; a number field gets a digits-only pad, and the field scrolls up so the keys never cover what you are typing. It appears only in kiosk (touch) mode and stays out of the screensaver's way. On by default; a kiosk with a keyboard attached can turn it off with the new On-screen keyboard switch next to the other display settings, a per-device choice.
- **Timers in the navigation menu.** Timers now has its own tab in the navigation menu, so the shared kitchen timers page is one tap away on every install, no Stream Deck or direct URL needed. Existing setups with a customised menu keep their arrangement; the new tab simply appears at the end of the menu, ready to be moved or hidden like any other tab.
- **Add a minute to a running timer.** Every running timer on the Timers page has a +1 min button next to Cancel, so a roast that needs a little longer gets it in one tap. The extension applies to the shared timer itself, so the Stream Deck and every other screen watching the countdown pick up the new time within moments. Timer buttons on the page are also bigger under kiosk mode, sized for a quick press with messy hands.
- **Screensaver on every browser.** A new "Screensaver on every browser" switch next to the screensaver settings extends the idle screensaver beyond the kiosk display: with it on, any browser viewing this install (a desktop or a phone included) dims to the gliding clock, or your photo slideshow, after the same idle minutes, with running timers floating along. Off by default, so kiosk screens keep the behaviour to themselves. The screensaver settings now also appear on server installs, where this switch is the way to use them.
- **Kitchen timers float on the screensaver.** While the screensaver is up, every running timer drifts around the screen as a small pill showing its name, a live countdown, and a food icon picked from the name (a Pasta timer shows a plate of spaghetti, Eggs an egg, and anything unrecognized a stopwatch), so a glance from across the kitchen tells you what is cooking and how long is left. The pills bounce off the screen edges, off each other, and off the gliding logo. A pill in its last minute breathes a pulsing pink glow to catch the eye, and a finished timer turns unmissable: it pulses red and amber, reads Done, spins slowly, and picks up speed until it is dismissed. Works with both screensaver styles, shows up to six timers at once (more collapse into a "+N more" note), and a touch still brings the page right back.
- **The screensaver can span the Stream Deck.** A new Screensaver position option in the Stream Deck settings tells Pantry Raider which side of the screen the deck sits on (above, below, left, or right). With a position set, the bouncing raccoon glides off the edge of the display and across the deck keys as if the two were one big screen, then bounces back; the keys stay dark while the logo is elsewhere. The deck joins the screensaver instead of blanking on its own, and pressing any key or touching the screen wakes both at once. Off by default and set per device.
- **A branded hello when the kiosk starts.** The first screen after a kiosk boots opens on a short intro: the Pantry Raider raccoon fades in on a dark screen, holds for a moment, then dissolves into the app. It lasts about two seconds, plays once per boot (moving between pages never repeats it), and a touch or key press skips it instantly. Screens set to reduce motion get a simple fade, and regular browsers never see it.
- **A quiet boot on kiosk screens.** A kiosk device no longer scrolls kernel and startup text across the screen while it boots; the display stays clean until the app appears. New devices get this from their first setup onward (the setup itself still shows its progress on screen), and already-installed devices pick it up with an update, taking effect from the next reboot after that.
- **Test the screensaver from Settings.** The Screen & Sleep pane has a Test screensaver button next to the save button: it starts the screensaver on that screen immediately with the style and speed picked in the form, so a slideshow or glide speed can be checked without waiting for the idle timeout (it works even while the screensaver is set to off). Any touch or key press dismisses it, and a kiosk panel is previewed by pressing Test on its own settings page.
- **The screensaver stays out of the way of cameras.** The idle screensaver no longer starts while the camera page is open or while a Home Assistant camera pop-up is on screen: watching a feed counts as using the display. The normal idle countdown starts fresh once the camera view is left.
- **Search box in Settings.** The Settings page now has a search box above the side menu. Type a few letters and the menu narrows to the pages that mention what you typed, searching titles, section headers, and field names across both the Settings and Personalization menus at once, so you no longer need to know which menu holds an option. Matching sections are outlined when you open a filtered page, and clearing the box brings the normal menu right back.
- **Start and cancel timers from the Timers page.** The Timers page now has one-tap preset buttons (1 to 60 minutes) and a custom timer with an optional name and your own minute count, so any screen can set a kitchen timer without a Stream Deck. Each running timer gets a Cancel button (Dismiss once it finishes), and everything you start or cancel here shows up, or disappears, on the Stream Deck and every other screen watching the same timers.
- **Photo slideshow screensaver.** The kiosk screensaver can now play your own photos: put images in a folder named photos or pictures at the top of a USB flash drive, plug it into the device, and pick Photo slideshow as the screensaver style in Settings, Personalization, Screen & Sleep. Photos fill the screen with a slow drifting pan, crossfade every 25 seconds in shuffled order, and a small clock hops between corners so nothing sits still on the panel. With no drive or no photos the screensaver simply shows the bouncing logo as before, so the setting is always safe to leave on. Per device, and a touch still brings the page right back.
- **Automatic backups to a USB flash drive.** Plug a formatted flash drive into the device and Pantry Raider can save backups to a pantryraider-backups folder on it, either on a schedule you pick in Settings under Backup & Updates (in hours, 0 turns it off) or with the new Back up now button. The pane shows whether a drive is detected, its free space, and when the last backup was written; the newest 14 backups are kept and nothing else on the drive is ever touched. A Pi appliance saves the same full stack snapshot as scripts/backup.sh (inventory, recipes, and app data), a satellite saves its device settings so a replacement unit can be restored from the drive, and a server saves the app-data backup zip.
- **The display can wake on motion.** Kiosk devices with the built-in accelerometer (the LSM6DSOX used for auto-rotation) can now wake a sleeping screen when the device is moved or bumped. A new "Wake on motion" option sits with Display sleep in Settings, Personalization, Screen & Sleep: Auto (the default) turns it on exactly when the sensor is fitted, with On and Off to force it either way. Devices without the sensor are unaffected, and a screen touch or a Stream Deck button press still wakes the display as before.
- **Manage Pantry does more than add.** The Add Food page is now Manage Pantry, with four tabs along the top: Stock up, Use stock, Shopping list, and Audit stock, the same names the Stream Deck mode key shows. The tabs are the shared scanner mode itself, so picking one switches every scanner at once: the USB scanner routes its next scan accordingly, the Stream Deck mode key updates, and if the mode is changed from the deck (or another screen) the page's tab follows within a few seconds. Consume takes items off Grocy stock by barcode, the shopping tab puts scanned or typed items on the Mealie list (with a pointer to Settings when Mealie is not connected), and the audit tab shows the running count and hands off to the full audit view.
- **Open on phone.** Manage Pantry has an Open on phone button that shows a QR code, so you can jump from a kiosk screen to your phone's camera and full keyboard in one scan.
- **Turn a Pi appliance into a satellite, and back, without reflashing.** A Pi Hosted appliance can now follow another Pantry Raider server: in Settings under Advanced, "Run as a satellite" takes the main server's address and API key, pauses the local Grocy and Mealie (every bit of their data stays on the device), and the screen and Stream Deck carry on backed by the main server. The switch is reversible: a switched device gets a "Switch back to full stack" button in its Advanced settings that restarts the paused stack and restores the inventory, recipe, and AI settings it had before, exactly as they were.
- **Plug in a display, get a kiosk.** A Pi appliance now notices when a screen is connected and sets the kiosk up on its own, even if the device was first installed headless: within about a minute of plugging in a display the kiosk provisions (or starts, if it was already installed) with no reflash or SSH needed. Setup shows the same picture: the Hardware pane and the wizard's Hardware step flag "Display detected, kiosk not set up" (and the Stream Deck equivalent) with a one-click Enable button, the wizard's display switch pre-fills from live detection, and the kiosk and Stream Deck service rows now tell "installed but not running" apart from "never set up" so a working install is never offered a reinstall. Setting ENABLE_KIOSK=false in the appliance config still opts a device out, and first-boot provisioning only skips the kiosk when no display is actually connected (an explicitly enabled kiosk installs even headless and starts once a screen arrives).
- **Live progress while Grocy installs on a new appliance.** On a Pi appliance's very first setup, the Grocy step now shows a scrolling window with the install's live output while the stack is still downloading and starting, instead of a note asking you to wait and retry. It tells you when Grocy is up and ready for its API key.
- **The Stream Deck boots with the Pantry Raider raccoon.** When the deck service starts, the keys now show the raccoon brand mark across the deck instead of the Elgato factory logo, until the real buttons appear a moment later.
- **Kiosk screensaver.** Settings, Personalization, Screen & Sleep has a new screensaver option for kiosk screens: after your chosen idle minutes the page dims and the Pantry Raider logo glides slowly around the screen with the clock and date riding along, and any touch brings the page right back. The constant motion protects the panel. Unlike Display sleep, which powers the screen off, the screensaver keeps the display on, so it suits panels that wake slowly or misbehave when switched off. A speed setting (slow, normal, fast) controls the glide. Off by default and set per device.
- **Start Page action keys fire on-screen.** Home Assistant toggle, media, and macro custom keys, plus the built-in HA slot keys (ha_1 to ha_5), now execute when pressed on the Start Page instead of asking for a connected Stream Deck. The server makes the Home Assistant call with the shared Stream Deck HA settings and the key shows the result in a toast. Macros run their HA slot and preset kitchen-timer steps (timers become shared server timers visible on every surface); deck-hardware steps are skipped and named in the toast.
- **AI token usage and budget.** Settings, AI & Scanning now tracks the tokens your AI provider spends through the app (this month, all time, and per provider) and lets you set a monthly token budget for your own API key. When the budget is reached, AI photo import and barcode enrichment are declined until the next month or you raise it. Usage is metered locally per instance and is the foundation for cloud per-user quotas.
- **Per-device Home Assistant on-screen events + connection status.** Each device now decides for itself whether to show Home Assistant notifications and camera pop-ups (a per-device choice, handy for a headless server or picking which Pi Remote displays them), overriding the server default. The Home Assistant settings show a connection status badge (configured / connected / not reachable) on both the server and satellites, and satellites get a Test connection button.
- **On-screen Start Page (optional).** A new full-screen launcher that works like an on-screen Stream Deck, at /ui/start. Choose 6, 15, or 32 keys (the keys scale to fill the screen without scrolling), arrange them by dragging actions from a palette onto the grid, and it replaces the fixed and floating menus while shown. Custom buttons are shared with the physical Stream Deck. Off by default; enable it in Settings, Personalization, Start Page & Stream Deck.
- **Weather page and settings.** The Stream Deck Weather settings are now just "Weather" and add an advanced weather-server option (the Open-Meteo API base, default the public service, so you can point it at a self-hosted instance). Weather also has its own navigation tab, so the forecast page is reachable without a Stream Deck.
- **Camera scan shows brand and resolution, and handles logins.** Scanning for IP cameras now labels each result with the detected brand and snapshot resolution, and a password-protected camera gets an inline username and password form that finds a working snapshot and lets you preview and add it.
- **Background image.** Set a photo behind the whole UI from Theme settings: upload an image or paste a URL, with an opacity slider so the interface stays readable. Applies on every page and device.
- **Named custom themes.** The custom theme builder now takes a name and saves your palette as its own entry in the Theme dropdown, so you can keep several and switch between them. A saved theme applies everywhere, including the Settings page itself, and can be deleted from the builder.
- **Reset navigation to defaults.** The navigation editor has a Reset to defaults button that restores the original tab order, folder grouping, and visibility in one click.
- **Hide the on-screen nav menu.** Settings, Personalization, Screen & Sleep now has an On-screen nav menu option: Auto (the default) hides the nav bar on a Stream-Deck kiosk at large or extra-large scale, where the deck does the navigating and a small panel is better used for content; Always show and Always hide are also available. The top bar keeps a hamburger menu so Settings is always reachable.
- **Update "last checked" time, timezone, and maintenance controls.** Backups & Updates now shows when updates were last checked, and Advanced adds a Date & time section to set the timezone used for timestamps (default follows the system NTP-synced clock; Pi Remotes inherit the timezone from the main server) and a Maintenance section with Reload settings (re-reads settings and rebuilds the AI/Mealie clients without a restart), Reboot now, and an optional nightly reboot schedule for a kiosk appliance.
- **A checklist to verify your Home Assistant setup.** The Home Assistant guide now ends with a step-by-step "Verify your setup" list covering the sensors, the barcode scanner, notifications, on-screen kiosk events, and the Lovelace dashboard, so after wiring up a new HA install (or upgrading it) you can confirm every part of the integration is alive without guessing what to poke.

### Changed
- **Snappier buttons and timers on small devices.** Kiosk screens and the Stream Deck now do noticeably less busywork per second, which matters most on a Pi 3 satellite where everything shares one small CPU. The deck only redraws keys whose face actually changed (a ticking timer no longer repaints the whole deck every second), a satellite answers a burst of timer lookups from every screen with a single trip to the main server, kiosk pages poll a little less often and skip polling entirely while their tab is hidden (countdowns still tick every second, computed locally), and the browser now caches the app's scripts and styles between page loads instead of re-checking each one. A press that cannot reach the main server also gives up and says so sooner instead of leaving the button hanging.
- **Recipe cards show in-stock coverage at a glance.** On What Can I Cook, the ingredient count chip on each recipe card is now colour-coded: green when nothing is missing beyond pantry staples, amber when at least half the ingredients are in stock, red below that. A bold "X of Y in stock" line with a slim coverage bar sits under the recipe name, so how close a recipe is to cookable reads without expanding the ingredient list.
- **Settings reorganized: two menus, grouped by what you're doing.** The Settings page now works like Plex or Jellyfin: a toggle at the top switches between Personalization, the things you change often, and Settings, the set-and-forget administration. Personalization (the default view) holds Appearance (theme, background image, navigation tabs), Screen & Sleep (everything about the screen: scale, rotation, sleep, screensaver, the on-screen nav bar, quiet mode, and the nightly kiosk reboot), Start Page & Stream Deck (both key editors side by side), and Recipe Preferences (staples, tastes, appliances, and suggestion thresholds). Settings holds Connections (Mealie, recipe sources, Home Assistant, cameras, remote access, and the phone QR code), AI & Scanning (the scanner, barcode enrichment, and the AI provider that reads photos and receipts), Inventory & Storage, Devices & Fleet, Security & Access, Backups & Updates, and Advanced. The page remembers which menu you used last, old links and bookmarks to any renamed section still land in the right place, the search box searches both menus at once and switches to the right one when you open a match, and every setting kept its save behaviour.
- **Manage Pantry adapts to the screen it is on.** On an attached kiosk display, which has no camera or file picker, the camera-scanner card and the Photo/Receipt tab step aside: the USB scanner is the input there, with a hint pointing at it and at the Open on phone QR code. A phone or desktop browser keeps the full set, with the live camera button appearing only when the browser can actually use a camera.
- **Project moved to the Syracuse3DPrintingOrg organization.** The repos now live under github.com/Syracuse3DPrintingOrg and the published Docker image moved to ghcr.io/syracuse3dprintingorg/pantryraider. Existing devices keep working: GitHub redirects the old repo URLs, and the updater rewrites a deployed compose file's legacy image reference on the next update (a device updating from a pre-move version needs two Update presses; the first refreshes the updater, the second migrates and pulls the new image).
- **Interactive demo refreshed.** The browser demo (docs/demo, deployed to Cloudflare) now carries the Pantry Raider branding (raccoon logo, favicon, watermark background), uses the current recipe tier names, and adds a Start Page screen showing the on-screen launcher whose Home Assistant, media, and macro keys fire without a Stream Deck.
- **README screenshots refreshed.** All six screenshots now show the current Pantry Raider UI (raccoon branding, current navigation, watermark background) and are regenerated by a new scripts/capture-screenshots.py, which boots the app against a built-in mock Grocy/Mealie with demo data and captures the pages with headless Chromium.
- **/ui shows the first page in your nav menu.** Visiting the app no longer always lands on the inventory dashboard; it opens whatever page leads your navigation menu. Combined with the Start Page defaulting to the top of the nav when enabled, you can make the Start Page your home screen.
- **Weather settings moved onto the Weather page.** Weather no longer has its own settings menu section; set the location, units, and (advanced) weather server with the gear button on the Weather page. The Stream Deck weather key still uses the same values.
- **Start Page editor matches the Stream Deck.** The Start Page editor now uses the same key catalog (all the same keys, grouped and coloured), the same shared custom-key library, and the same key style and icon options as the Stream Deck, with the roomier Start Page key grid. Both editors now read the exact same action catalog (the live one from the deck host bridge), so the palettes and keys are identical. The full-screen Start Page renders the keys in the same coloured deck style. Custom keys are one shared library: build or edit a key on either side and it shows up on both, with the Stream Deck placements preserved.

### Fixed
- **Timers, the current recipe, and on-screen notifications now stay consistent and survive restarts.** These used to live only in the memory of the server process answering the request, so on a server running more than one worker a timer started through one could be invisible to the screen polling another, and a Home Assistant notification could vanish before the kiosk saw it. They are now kept in shared state that every part of the server reads, so every screen and the Stream Deck always agree, and running timers and queued notifications carry across an app restart or update (a timer keeps its original finish time, so the countdown stays honest). The server also warns in its log at startup when it detects more than one app process sharing the same data directory.
- **Every page says so honestly when its backend is down.** When Grocy, Mealie, or (on a satellite screen) the main server cannot be reached, pages no longer show raw error text, endless spinners, or misleading empty states: the inventory dashboard, Expiring, Cook, Recipes, Meal Plan, Shopping, Pending, Audit, Journal, and the Manage Pantry tabs all keep their page up and show a plain banner such as "Grocy is not reachable. Inventory will return when it is." An outage never disguises itself as good news anymore: Expiring no longer celebrates "nothing expiring", Shopping no longer claims there are no lists, and a consume scan no longer blames the barcode. Everything returns on its own once the service is back.
- **The settings pages now require a login on protected installs.** The setup wizard's pages skipped the password check permanently, not just during first-time setup, so anyone on the network could change settings on a password-protected install. The wizard still works before any credentials exist; once setup completes, the settings surface requires the same login as everything else. The kiosk's own screen is unaffected.
- **Tune My Suggestions icons show everywhere.** Some icons in the Cook page's Tune My Suggestions panel (the Midwest Mayo jar, the Mediterranean olive, and the flag icons on Greek, Korean, Filipino, and British) came from newer emoji sets that Windows and older devices cannot draw, so they showed as empty boxes or bare letters. They have been replaced with equally fitting icons every platform supports. Pi kiosk screens, which had no color emoji font at all and showed boxes for every icon, get the font installed on new setups and picked up by existing devices on their next update.
- **Start Page timer keys show the live countdown.** A timer key now works exactly like its Stream Deck twin after starting a timer: the key face becomes the ticking countdown, brightens while running, pulses when the timer finishes, and a press dismisses it. The face tracks the shared registry, so a timer started from any device shows on the key.
- **Start Page timer keys start, extend, and reset timers.** Pressing a timer key on the Start Page used to just open the Timers page; it now works like a kitchen timer key: a press starts the timer (the preset minutes, or 1:00 on a plain Timer key), each press while it runs adds a minute, a press on a finished timer dismisses it, and holding the key resets it. The key face shows the live countdown. Timers land in the shared registry, so they appear on every device; the Timers key still opens the full page.
- **The Start Page editor shows every key on server installs.** On a server without a Stream Deck attached, the Start Page editor offered a shortened, out-of-date key palette: newer keys like Ready, Cooked, Scan Mode, Check Off, Convert, Timers, the Eggs/Pasta/Rice timer presets, Clock, Tonight, the recipe scale keys, and the Camera keys were missing, and the pantry key still carried its old Add label. The full key catalog now ships with the app, so the editor and the Start Page show the same complete palette everywhere, matching what a Pi appliance gets from its deck. The new keys open sensible pages when pressed on screen: Ready opens Cook, Cooked and the scale keys open the Current Recipe, Scan Mode opens Manage Pantry, Check Off opens the shopping list, and Health opens Setup (where the deck sends it too).
- **Kitchen timers are now shared with satellite screens.** A timer started on a Pi Remote (from its Timers page, its Stream Deck, or a recipe step) used to run only on that device, invisible to the main server and every other screen. Timers now live on the main server no matter where they are started, so the Timers page, the screensaver timer pills, and the Stream Deck on every device show the same countdowns, and a timer started at the stove can be cancelled or extended from any other screen. If the main server cannot be reached, a Stream Deck key still falls back to its own local countdown so the kitchen timer keeps working.
- **An audit stock count no longer loses scans on a busy server.** The audit session (the location lock, the expected stock snapshot, and every scan recorded so far) now lives in shared state that every part of the server reads, so counts taken while the server is handling other work stay consistent instead of a scan occasionally landing in a session the server had already forgotten. A pleasant side effect: a half-finished count now survives an app restart, so an update or reboot mid-audit no longer throws the count away.
- **The Wi-Fi setup hotspot watchdog now receives updates.** The small script that turns on the FoodAssistant setup hotspot when a device boots with no network was written once when the device was imaged and never touched again, so improvements to it never reached deployed devices. It now ships with the app and refreshes on every update, like the other on-device helpers.
- **The Stream Deck idle blank timeout now works.** The "Blank after idle" minutes in the Stream Deck settings were saved but never delivered to the deck, so the keys stayed lit no matter what the setting said. Saving the deck settings (and, on a satellite, the regular sync with the main server) now writes the timeout through to the deck, which blanks after the chosen minutes and wakes on any key press or screen touch as intended. If the screensaver spans the deck, the deck joins the screensaver instead of going dark.
- **The screensaver logo reaches every edge on scaled displays.** On kiosks with an interface scale other than 100%, the bouncing screensaver logo turned around before reaching the right and bottom edges (or drifted past them), because the bounce measured the screen in one unit and moved the logo in another. The walls now line up with the true screen edges at every scale and rotation, so the logo kisses all four sides like it should.
- **The phantom kiosk cursor is gone at the source.** The Pi's HDMI CEC remote-control devices announce themselves with mouse-style movement axes, which made the display server treat them as a pointer and draw a cursor no theme or page styling could remove. Those devices are now ignored as input (the kiosk never uses CEC input); a real mouse still works when plugged in. Applied on fresh installs and delivered to existing devices with the next update.
- **The kiosk cursor is really gone now.** The CSS that hides the pointer on a kiosk was gated on the browser reporting a touch-style pointer, but touch panels and barcode scanners expose phantom mouse handlers that made the browser report a precise pointer, so the cursor stayed visible on exactly the devices the rule was for. The hide is now unconditional in kiosk mode.
- **Updates apply new update steps in the same press.** When an update shipped a newer updater, the rest of that run still executed the old version, so steps added in the new one (like the hidden-cursor retrofit) only applied on the NEXT press. The updater now re-runs its fresh copy immediately after replacing itself, so one Update press is enough.
- **Stream Deck weather tiles now say why they have no forecast, and stop crowding the weather service.** A weather key whose custom location the weather service could not find showed the same "No signal" as a dead network, so there was nothing to act on. Failed tiles now show the actual reason: "Bad location" when the place name is not recognized (fix the spelling or use a lat,lon pair), "Weather busy" when the service is rate-limiting, "No network" when nothing is reachable, and "No data" for a garbled reply. Forecasts are also cached on the server for ten minutes per location, so several weather keys and the weather page share one fetch instead of each asking the internet separately, and the deck refreshes all its weather keys at once with a strict time limit per request, so one slow lookup no longer holds up the rest of the deck. Together these also stop a satellite's weather keys from tying the device up in stalled requests.
- **Stream Deck timers and the Timers page are finally the same timers.** Every timer key on the deck now runs through the shared timer list: pressing Timer 1/2/3 (which cycles 5, 10, 15, 30, then 60 minutes, matching what the key always claimed) or a preset key like Eggs starts a timer that appears on the Timers page and the floating timer window right away, and holding a deck key to reset it clears it from every screen too. It works in both directions: cancel a timer in the browser and the deck key goes idle on its next update, start a Pasta timer from the web and the deck's Pasta key picks up the countdown. The keys count down from the same shared clock as every other screen, keep their flashing done alert (dismissing it on the deck clears the finished timer everywhere), and if the deck ever loses contact with the app a pressed timer still counts down on the deck itself.
- **No more mouse cursor on the kiosk.** Kiosk screens now hide the pointer by default: the old behavior kept it visible whenever a pointing device was detected at install time, and USB barcode scanners announce themselves as a mouse, so scanner-equipped kiosks showed a stray cursor (including over the screensaver). Already-deployed devices pick the hidden cursor up on their next update. If you actually use a mouse on the kiosk, set HIDE_CURSOR=false in the device config to keep the pointer.
- **The phone QR code now points somewhere a phone can reach.** On a kiosk the browser opens the app at localhost, so the "Add items from your phone" QR code (and the QR nav icon) encoded an address that went nowhere when scanned. The code now swaps a localhost address for the device's own network address, and a new Phone QR code address option in Settings, Connections lets you encode a public URL instead, for phones that reach the app through a reverse proxy or tunnel.
- **"Use it up" on the Expiring page now leads somewhere.** The button is now "Use-it-up ideas" and makes its outcome clear: the ideas panel scrolls into view, names the expiring items it is targeting, and each AI recipe idea gets a Cook this button that opens the Cook page with the full recipe generated and ready to save or start. When no AI provider is set up, the panel says so and still shows the quick tips (a provider hiccup used to fail the whole request). And the green checkmark next to each expiring item, the button that actually consumes stock, now asks for confirmation first and names the product in its message, so nothing leaves your stock unannounced.
- **Consuming by scan now works, including for items already in stock.** Items added through the app never had their barcode recorded in Grocy, so switching the scanner to Consume and scanning the same product answered "could not find barcode". New imports link the barcode automatically, and scanning an older product in consume mode looks it up, links the barcode to the matching product, and consumes it in one go.
- **The Stream Deck and Start Page "Add" key is now "Pantry".** It opens the Manage Pantry page, matching the renamed navigation.
- **Snoozed and archived alerts stay put.** Snoozing or archiving an expired-food alert only lasted until the next inbox refresh: the alert generator saw the product was still expired and flipped the item straight back to open. Snoozes now hold until their wake time, archives stick, and when an item genuinely resolves (the product is consumed or its date moves out) and later expires again, a fresh alert appears instead of being swallowed by the old one.
- **The Stream Deck mode key tells you when the mode did not change.** Pressing the scanner-mode key on a deck whose main server could not take the change (an older server version, or a satellite that cannot reach it) used to look like a normal press while every scan kept its old routing. The press now reports the failure in the deck log instead of pretending, and a successful press paints the new mode on the key immediately.
- **The scanner mode is now the same everywhere, and it remembers itself.** On a server running the app across several worker processes, the barcode scanner mode (Stock / Use / Shop / Audit) could be changed on one worker while a scan landed on another, so a scan meant to use an item up could quietly queue it as new stock instead. The mode is now shared by all workers, and as a bonus it survives an app restart: the scanner comes back in the mode you left it, instead of resetting to Stock.
- **Open Grocy and Open Mealie links on a satellite stay on the LAN.** On a Pi Remote, the Open Grocy and Open Mealie buttons could send the browser through the main server's public address (and its sign-in proxy) even though the server is right there on the same network. Those links now use a local address: the pulled backend URL when it is already LAN-reachable, otherwise the main server's LAN address on the backend's own port, with the public address kept only as a last resort.
- **Custom Stream Deck keys placed past the first page now work.** A custom key dropped on page two or later of the layout editor (a quick-add shopping item, timer, or any custom button) was silently dropped by the deck, showing the stock key or a blank instead. The deck now paginates exactly like the editor grid, so a custom key fires from whichever page you put it on, even when other keys around it were left blank.
- **Macro keys survive a save.** Saving Stream Deck settings turned a macro key's list of actions into one unusable text blob, so the macro ran zero steps from then on. The saved config now keeps the action list intact.
- **Screen rotation reaches a DSI display.** Applying a rotation only rotated the first display the compositor listed, which could be an unused HDMI connector on a Pi driving the official 7-inch DSI panel, leaving the panel itself unrotated. Rotation is now applied to every display the kiosk compositor reports, so a DSI panel rotates the same way an HDMI screen does.
- **Touch calibration no longer freezes the display.** Applying (or resetting) a calibration restarted the kiosk in the middle of serving the calibration page, which could strand a slower Pi on the boot console. The restart is now scheduled a moment after the page gets its answer, and the calibration screen shows a "restarting the display" state while it happens.
- **The first calibration tap always registers.** Tapping a crosshair in line with your previous touch could be silently ignored, because the touch panel does not resend a coordinate that has not changed. The calibration reader now remembers the last known position, so every tap counts, including the first one after a rotation change.
- **The kiosk display comes up reliably on boot.** On first boot (and on slow starts) the browser could launch before the app was answering, showing a connection error that never retries, or give up entirely after early crashes while the display stack was still coming up. The kiosk now waits briefly for the app before launching, never stops retrying, and starts after the seat manager it depends on. Existing devices get the same hardening automatically on their next update.
- **A portrait kiosk no longer runs off the right edge of the screen.** On a rotated 7-inch panel the bottom navigation bar overflowed sideways with half its icons unreachable, and the Inventory and Cook toolbars pushed the whole page onto a horizontal scroll. The nav bar now wraps its icons onto extra rows on a narrow screen, the toolbars wrap instead of overflowing, and in kiosk mode the page itself never scrolls sideways at any rotation or interface scale.
- **No more Wi-Fi setup banner on a wired device.** A Pi connected over ethernet could still show "Running in Wi-Fi setup mode" (and keep broadcasting the FoodAssistant hotspot) if the fallback hotspot had ever come up. The device now double-checks its real connectivity when the settings page asks about setup mode: with a wired link or any working network route, the hotspot shuts down on its own and the banner stays hidden.
- **Fresh Pi installs get Mealie by default again.** The install script quietly turned Mealie off on a fresh Pi Hosted (or server) install even though a hosted device is meant to ship with recipes and meal planning ready to go. Mealie now installs by default on a full local stack; set ENABLE_MEALIE=false at install time to skip it. New installs also record their enabled services in the stack's .env, so later docker compose commands and updates keep Mealie in the stack.
- **The first Mealie install survives interruptions.** Starting Mealie from Settings downloads its app image, which can take several minutes on a Pi. That download now keeps running on the device if you leave the setup page, the page reconnects to a start already in progress when you come back (instead of sitting idle until you click Start again), and if the device reboots or the helper restarts mid-download, the install resumes by itself. The Mealie section also says what to expect before you press Start.
- **The Stream Deck raccoon shows for the whole startup, not a blink at the end.** The boot splash used to paint only after the deck service finished loading, so the Elgato factory logo sat on the keys for most of startup and the raccoon flashed just before the buttons appeared. The splash is now the first thing the service puts on the keys, before the heavy loading, and it stays up until the real buttons replace it. The deck's own power-on logo cannot be changed: the hardware has no supported way to store a custom image, so the factory logo still shows for the moment between power-on and the service starting.
- **Barcode scans no longer yank the kiosk to another page.** A USB barcode scanner types its code as a fast keystroke burst, and the keyboard nav shortcuts treated the first digit as a "jump to tab" press, so every scan flung the screen to Inventory (or whatever tab owned that number) before the scan could be processed. Number shortcuts now wait for a brief quiet moment before navigating, so scans are captured and routed by the scanner mode while deliberate single key presses still work.
- **Stream Deck weather tiles no longer show "No signal" while the weather page works.** The deck fetched wttr.in directly, which is often rate-limited; the tiles now get their forecast from the app, which prefers Open-Meteo (including a self-hosted weather server) and falls back to wttr.in. Per-key weather overrides also honor their own units now.
- **Display rotation survives a restart on devices imaged before late June.** Older images have a kiosk service that never re-applies the saved rotation on startup, and updates never refreshed it; the updater now patches that in. The rotation helper also waits longer for the display to come up on slow boots, where a 10 second window silently lost the rotation.
- **Clearer message when a device helper is out of date.** Pressing Reboot now on a device with an old helper daemon showed a bare "not found"; it now says to run Update and try again. (The reboot itself was fixed by the update fixes above: the helper daemon was frozen at an old version.)
- **Missing device tools now install themselves.** On a device imaged before a helper tool existed, display rotation, the Stream Deck screen-off key, and restore could fail because the tool was never installed. The device now reinstalls a missing tool from its own update source on the spot and carries on; only when that is impossible does it show a message saying what to press instead. The screen-off key also stops pretending it worked: when the display truly cannot be switched, the key shows Failed instead of Off, and touch calibration on a not-yet-updated device now says to press Update rather than "Helper not installed".
- **Touch now follows display rotation on never-calibrated screens.** On panels whose touch is accurate out of the box (like the official 7-inch DSI touchscreen), rotating the display left touch in the original orientation, because the counter-rotation was only applied on top of a saved calibration. Rotation now writes the correct touch matrix even when the screen was never calibrated, applies it on the reboot path too, keeps it consistent if the rotation command fails, and re-applies the right orientation after a calibration reset on a rotated display.
- **Updates now refresh every device helper, not just some.** The on-device updater used to refresh only itself, the restore helper, and the host bridge; the display power and rotation helpers were never installed or updated, so a device imaged before a helper existed never received it (stale rotation and missing screen-off controls on older Pis). Every update now syncs the full helper set, including helpers added after the device was imaged.
- **Updates recover from a rewritten GitHub history.** If the project history is force-pushed, the updater's fast-forward pull could never succeed again and the device was silently stuck on the old version forever. The updater now detects this, resets its update source to match GitHub exactly (the running app and its data are untouched), and reports the recovery in the update result.
- **Update check no longer shows a stale "up to date".** The version check now bypasses the GitHub raw CDN cache, so Check for updates sees a new release right after it is pushed instead of a few minutes later.
- **Theme contrast fixes.** Several bundled themes had unreadable spots: the selected side-menu item showed accent-on-blue (worst on iOS Light), the Save buttons were a washed-out cyan on light themes, and status badges used white text on bright backgrounds. These now use legible pairings on every theme. Also removed stray markup that had leaked into three theme stylesheets and was breaking their later rules.
- **Custom themes now take effect.** Selecting or saving a custom theme correctly recolours the whole app, including the Settings page, instead of appearing to do nothing.
- **Settings opens straight to the right menu.** The page no longer flashes the Settings menu and then jumps to Personalization on load.
- **Dragging a tab back into a folder works.** The navigation editor now nests reliably: the middle of a row drops a tab into a folder (the top and bottom edges reorder), and dropping onto any item already inside a folder adds it to that folder, so a tab moved out of a group can be dragged back in.
- **Waveshare resistive HDMI touchscreens now register.** A resistive Waveshare 3.5-4 inch HDMI panel uses an ADS7846 SPI touch controller that stays invisible until SPI and its overlay are enabled. Choosing the display type in setup after first boot never wrote those, so touch was dead and the kiosk reported "No touch device detected". Saving the display type now applies the overlay (with an Apply touch driver button under Settings, Personalization, Screen & Sleep), and the wizard help points resistive panels at the ADS7846 option instead of the USB-touch one. A reboot loads the overlay.

## [0.7.0] - 2026-06-30

### Added
- **Quiet mode and a timer chime.** A finished kitchen timer now plays a short chime in the on-screen timer window so it carries across the room. A per-device Quiet mode toggle (Settings, Interface) silences it, leaving the highlighted timer row as the only signal, so one kiosk can be loud and another silent.
- **Release notes link and a manual server update.** The Settings Updates card links to the GitHub release notes on every deployment mode. A non-Pi server, which runs Watchtower on a daily poll, gains an Update now button that triggers Watchtower immediately so an available image is applied at once instead of waiting for the next poll, with the copy-paste commands kept as a fallback.
- **Recommended kitchen products (Shop tab).** A new Shop page recommends common kitchen products (appliances, cookware, gadgets, storage) as Amazon links. Items you have not marked as owned in your kitchen appliance list, and any equipment your active recipe needs but you lack, are pinned to the top; the rest are popular general picks. Add your own Amazon Associates tag under Settings > Recipes to monetize the links (qualifying purchases earn a commission); the tag is shared to satellites. The page carries the required Amazon Associate disclosure, and links open in a new tab. This is not an AI feature, so it works without any provider configured.
- **Drag-and-drop navigation editor with folders.** The Settings nav editor replaces the per-row parent dropdown with a tree you can rearrange: drag a row to reorder it, drop it onto a top-level tab (or use the indent button) to nest it, and outdent to bring it back. Because the kiosk is touch-only, every row also has move up, move down, indent, and outdent buttons. A new Add a heading control creates a folder (a label and icon with no page of its own) that groups other tabs into a dropdown; an empty folder stays hidden until it has children, and every page remains reachable.
- **Pending duplicate hint.** When a scanned item is already in Grocy inventory, the Pending page shows a small "Already in inventory (duplicate)" info badge on that row. It is informational only: the item can still be committed, and an item scanned on a different day lands as its own Grocy stock entry (Grocy keys entries by best-before date) so each keeps its own expiration.
- **Home Assistant on-screen notifications.** Turn on the event channel under Settings > Home Assistant and a Home Assistant automation can push notifications to this device's screen (a `rest_command` to `/events/notify`); they appear as toasts on the kiosk and in any open browser tab, coloured by level. The settings page shows the exact rest_command and automation YAML and has a Send test notification button.
- **Home Assistant camera pop-ups.** An automation can pop a camera up full-screen on the display (`/events/camera-popup` with a camera name), for example the doorbell camera when a person is detected. It shows for a configurable few seconds, then closes; it reuses the same camera proxy as the Camera page.
- **Convert has its own tab, and is customizable.** The Conversions page is now a normal navigation tab (hideable like any other), and a "My conversions" section lets you add your own quick-reference rows (for example "1 stick butter = 113 g") that stay on the device alongside the built-in cheat sheet and the calculator.
- **Stream Deck custom keys are a drag-and-drop library.** Custom keys are now created once in their own section (no slot number to type), and each appears as a chip in the palette under the grid. Drag it onto any key to place it, exactly like a built-in action; a custom key left unplaced is kept in the library for later. The grid shows each placed custom key's real face, and its row notes which key it sits on.
- **Stream Deck Home Assistant media keys.** A Media override type binds a key to a Home Assistant media_player and a transport action (play/pause, next, previous, volume up/down, stop). It fires the service on press with no on/off polling, reusing the shared Home Assistant connection.
- **Pantry audit.** A new Audit tab runs a read-only, location-scoped stock count: lock it to one storage location, scan the items there, and the page shows the expected stock (from Grocy) against what you scanned so missing and unexpected items stand out. Nothing is written back to Grocy. On a satellite the scans forward to the main server, so every surface sees one session. Audit is also a fourth barcode scanner mode (see below).
- **Nutrition tracker.** A new Nutrition tab logs what you eat with calories and macros (protein, carbs, fat) and shows daily and recent-day totals. When an AI provider is configured, an estimate button fills in the macros from a food name.
- **Kitchen Guide reference page.** A new Kitchen Guide tab collects quick kitchen reference material alongside the Convert tab.
- **Satellite update badge.** The Satellite Devices pane in Settings shows each remote's reported version against the main server's, with an up-to-date or behind badge so it is obvious which satellites need a `sudo foodassistant-update`. Each satellite reports its version on every config pull.
- **Finish setup from your phone.** When the setup wizard is opened on an attached kiosk display before setup is finished, it offers a phone or laptop URL (with a QR code) so you can fill the many text fields on a real keyboard instead of the touchscreen, with a Continue on this screen button if you prefer the kiosk. The URL uses the device's LAN host, not the kiosk's localhost.
- **Stream Deck barcode scanner modes.** A scan-mode key cycles the scanner context (Stock, Use, Shop, Audit) and shows the active mode on its face, so one physical scanner can add to inventory, consume stock, add to the shopping list, or run a pantry audit. The mode lives on the main server, so a satellite's deck and the server agree.
- **Version bump tooling.** `scripts/bump-version.sh [patch|minor|major]` edits the single source-of-truth `APP_VERSION`, and `scripts/install-git-hooks.sh` installs a pre-commit hook that auto-bumps the patch on each commit so every commit changes at least the patch number. The hook stays out of the way during rebases, merges, and explicit minor/major bumps.
- **Kiosk display sleep with shared wake.** The kiosk screen now blanks after its idle timeout (previously a stored but unused setting) and wakes on a touch, key press, mouse move, or a Stream Deck button press. The display and the Stream Deck keep separate timeouts, but activity on either surface wakes both: the host bridge owns the display blanking and brokers activity between the screen and the deck. Manual blank/wake and the idle timeout are exposed through the bridge.
- **On-screen floating navigation menu.** An optional column of nav icons docked to a screen corner, handy on touch screens. Drag its handle to reposition it; it snaps to the nearest corner and remembers the spot per-device. A setting in Interface picks the default corner (or off), and it can auto-hide when a Stream Deck is connected, since the deck already provides navigation. The menu can be laid out vertically or horizontally per-device, and the page content is padded so the menu never sits on top of the text.
- **Restore from backup.** The Backup pane now has a Restore control that rebuilds this app's data (settings, database, staples) from a backup zip, the counterpart to Download Backup. The current data is copied aside first, archive paths are validated against zip-slip, and a redacted backup keeps the API keys you already have. Grocy and Mealie data are not touched (use the host script for a full snapshot).
- **Full Grocy + Mealie restore on a Pi.** On a Pi appliance the Backup pane gains a full-stack restore that runs through the host bridge: point it at a `.tar.gz` already on the device or an `rclone:` remote path, and the bridge stops the stack, swaps the data dirs aside, unpacks, and restarts. The archive is validated before the stack is stopped, and a failure mid-restore still brings the stack back up. This is distinct from the in-app app-data restore above.
- **Current Recipe.** A new On the Line tab (the active recipe) loads one active recipe (from a Mealie recipe, an imported recipe, or an AI-generated one) and keeps it on the server so every surface agrees. It shows the ingredients and steps, scales servings, and turns durations written in the steps (for example "simmer 20 minutes") into ready-to-start named timers. Timers live on the main server, so the web UI, an attached Stream Deck, and satellites share the same countdowns. A floating on-screen timer window shows running timers and steps aside when a Stream Deck is present, and the deck's timer keys auto-populate from the active recipe, labelled per step.
- **Launch a recipe as the Current Recipe.** A Cook button on each Recipes row and on the Cook page suggestion tiles (and a Cook this action in the AI recipe preview) makes that recipe the active Current Recipe, instead of Recipes only linking out to Mealie.
- **Recipe import from a file.** Import a recipe from a generic recipe JSON, a schema.org Recipe JSON-LD file, or a Mealie export, in addition to the existing import from URL and from a photo.
- **Custom AI prompt on the Cook page.** An optional, collapsible prompt box steers the AI suggestions and the full recipe the AI generates; empty means the default prompt.
- **More themes and a theme builder.** Three new built-in themes (Solarized, Midnight, Forest) plus a Custom theme builder in Settings > Interface that lets you pick your own palette swatches. Stream Deck key palettes follow the active theme.
- **Richer Stream Deck override editor.** The per-key override editor now previews each override on the grid in place, lets Home Assistant action keys set their own on/off colours and an icon, and lets a weather key show its forecast (high/low) tile.
- **On-demand camera feeds.** Configure camera feeds (a live HLS or MJPEG stream plus a still snapshot) under Settings > Interface > Cameras. A new on-screen Camera page shows the live feed, and a connected Stream Deck can show a snapshot key or splash the snapshot across the whole deck (a periodic still, not live video, since the deck is a slow USB-HID surface). Any key press exits the full-deck view.
- **Home Assistant lives on the server, with camera discovery.** The Home Assistant URL and long-lived access token are now set once under Settings > Interface > Home Assistant and stored on the main server, so they can be entered from the server or a Pi, are one source of truth, and are inherited by a second Pi remote without re-entering them. A Discover from Home Assistant button lists the instance's camera entities and adds them with their stream and snapshot URLs built for you. The Stream Deck Home Assistant keys reuse the same shared credentials.
- **Camera in the navigation.** When at least one camera is configured, a Camera entry appears in the navigation bar, the floating nav, and the overflow menu, so the live feed page is reachable without typing its URL. It hides itself again when no cameras are set.
- **Dedicated Home Assistant and Cameras settings pages.** Home Assistant and Cameras moved out of the Interface pane into their own entries in the Settings menu, so they are easy to find and have room to grow.
- **Add a camera by IP.** The Cameras page can build a network camera's stream and snapshot URLs from its address, with brand templates for Generic MJPEG, Generic snapshot, Reolink, Amcrest/Dahua, Hikvision, and ONVIF, plus a Custom path. It fills a camera row you can review and edit before saving. RTSP-only cameras still need an MJPEG/HLS source or a transcoder, which the page notes.
- **Choose which camera a Stream Deck key shows.** A new Camera override type in the per-key editor binds a key to a specific configured camera (by name) instead of always the first one, and an optional Full deck flag makes that key splash the chosen camera across the whole deck on press. Several camera keys can each show a different feed.
- **Weather page on the display.** Pressing a Stream Deck weather or forecast key now opens a full forecast page on the attached kiosk display (in addition to cycling the key face), so the deck doubles as a remote for the screen. The page is reachable at /ui/weather and uses the same location and units as the deck weather widget. The forecast comes from Open-Meteo (free, no key) with wttr.in kept as a fallback, since wttr.in is frequently rate-limited and was the likely cause of the page reading "unavailable".
- **Custom navigation tabs and nested submenus.** Settings > Interface can now add your own top-level navigation entries (a label, icon, and a root-relative or external URL) and nest tabs under a parent so the bar groups into dropdown menus. Both built-in tabs and custom tabs can be nested one level deep, and the existing order and hidden-tab controls still apply. Navigation layout is per-device, so each kiosk can arrange its own menu.
- **Fleet-wide automatic updates.** An "Install updates automatically" setting (on by default) now drives updates across a whole deployment. A Pi appliance applies updates through the host-bridge over-the-air helper; a non-Pi server applies them through the bundled Watchtower container. The flag is a single global setting that Pi Remotes inherit from their main server, so a server and its satellites converge on the same version instead of drifting apart.
- **In-app updater on Pi Hosted.** The in-app update control now works on a Pi Hosted appliance, not only a Pi Remote, so a full-stack Pi can check for and apply an over-the-air update from its own Settings page.
- **Debug logging with a downloadable bundle.** A debug logging toggle under Settings > Security raises the app log level and writes a rotating log file under the data directory. A Download control hands you that log for support, with secret values redacted. It is off by default.

### Fixed
- **Custom camera Stream Deck keys open their own camera.** A camera key set to a specific camera showed the right glyph but opened the first camera on the kiosk screen, because the press dropped the camera name and the Camera page always started on camera 0. The key now carries its camera through as a `?cam=` query param and the Camera page opens that feed (by name or index), falling back to the first camera only when none is requested.
- **"With pantry staples" recipes show up now.** That Cook bucket was usually empty because real recipe ingredients carry measurement and quantity words ("3 tablespoons unsalted butter", "1 teaspoon kosher salt"); those extra words stopped common pantry items from being recognised as staples, so the recipe fell into "needs shopping" instead. Measurement and quantity words are now ignored when matching staples, and the built-in staples list is broader, so recipes you can make from stock plus pantry basics land in the right bucket.
- **Wired Pi no longer drops into Wi-Fi setup mode.** The fallback setup hotspot used to start whenever Wi-Fi was not associated, even on a Pi with a working Ethernet connection. It now stays off when any other interface provides connectivity (a default route, or a wired interface that is up with an IP). The Network pane also shows an Ethernet "Connected" badge instead of reading as offline when Wi-Fi is idle.
- **Home Assistant cameras now display.** HA camera feeds showed "Camera unavailable" because the discovered URLs put the long-lived token in the query string, which Home Assistant rejects (it wants an Authorization header a browser cannot send). Cameras are now bound to their HA entity and fetched with the proper bearer header: the app proxies them for the on-screen Camera page, and the Stream Deck fetches them directly with the header. Cameras you already added are recovered automatically from their stored URL, so no re-adding is needed.
- **Phone QR code stays in kiosk mode.** The QR code that opens the UI on a phone is no longer hidden in kiosk mode, where it is most useful (scan the wall-mounted screen to control it from your phone).
- **Home Assistant and cameras now sync to a Pi Remote.** A satellite mirrors the main server's Home Assistant credentials and camera feeds, and its Settings show them read-only with a "configured on the main server" note (like the Stream Deck weather), so the values are visible and clearly server-managed instead of looking unset. Update the satellite (`sudo foodassistant-update`) so it pulls the new fields.
- **Kiosk overflow menu was nearly empty.** In kiosk mode the three-dots More menu hid everything except Settings (the reference links are kiosk-hidden and the secondary-tab copies only appeared under 820px). The secondary destinations (Recipes, Cook, On the Line, Meal Plan, Camera) now show in that menu in kiosk mode at any width, so every page stays reachable from the kebab.
- **Display scale applies without a reboot.** Changing the display scale or orientation from a phone or laptop now restarts the kiosk browser so the attached display picks it up right away, instead of waiting for a reboot.
- **Touch calibration page loads.** The full-screen touch-calibration page was crashing (a deprecated template call), so calibration could never start; it renders now.
- **Satellites survive a flaky mDNS.** A Pi Remote caches its main server's LAN IP on each successful sync and falls back to it automatically when the configured `.local` name stops resolving, so the satellite stays wired to its server on networks that block or drop multicast DNS. Device discovery (the Scan LAN button) was already IP-based, and the co-hosted Grocy/Mealie browser links already prefer the LAN IP.
- **Over-the-air Pi updates now refresh the Stream Deck too.** The update helper redeploys both the web app and the Stream Deck controller package (previously only the app), reinstalls Python dependencies only when they changed, restarts both services, and is safe to re-run after a manual `git pull`.
- **No mouse cursor on a touch kiosk.** Fullscreen Chromium painted its own arrow over the page on a touch-only kiosk display. The kiosk stylesheet now hides the cursor over web content, so a wall-mounted touchscreen shows no stray pointer.

### Changed
- **Settings menu reorganized into logical groups.** The Settings sidebar now groups its sections under Services, App, Devices & Hardware, and System headers instead of a flat list, so related settings sit together and the flow reads top to bottom. The satellite (Pi Remote) and Pi-only visibility rules are unchanged: a satellite still shows Main Server in place of the backend services, and Display/Stream Deck/Network stay Pi-only.
- **Per-section saving in Settings.** The single "Save All" button is gone. Each settings section now has its own Save button that stores only that section's fields, so saving is explicit and scoped and one section's edits never carry another's. Options that apply instantly (like the theme preview and per-device toggles) keep working as before.
- **Cook suggestions match pantry staples better.** Recipes whose ingredients carry descriptor words (for example "parmesan cheese" or "grated parmesan" against a "Parmesan" staple) now match, so the "Ready to cook" and "With pantry staples" buckets populate from current stock instead of coming back empty.
- **Readable Stream Deck key labels.** Label text colour is chosen from each key's background brightness, so themed keys (for example a light-green Commit) stay legible instead of washing out white-on-light.
- **Co-hosted Grocy/Mealie browser links use the LAN IP.** The "Open Grocy/Mealie" links now prefer the device's LAN IP over its `.local` name, so they work on networks where mDNS does not resolve. The loopback address is still used for the behind-the-scenes API wiring.
- **AI options hide when no AI is configured.** The Ask AI button, recipe-from-photo import, the Add page's Photo/Receipt tab, and other AI-only affordances are hidden across the UI until a vision/LLM provider is set up, so the interface never offers actions that cannot work.
- **Small-screen kiosk view.** On small screens (for example an 800x480 panel) the secondary nav tabs collapse into the overflow menu with larger touch targets, and the layout simplifies to a single column. On a Pi with a display attached, kiosk mode now enables itself (respecting an explicit choice to turn it off).
- **Barcode enrichment model picker.** The enrichment model is now a provider-aware dropdown (matching the main AI model picker) with a free-text override, instead of a plain text box.
- **Cook suggestion factors can be toggled.** Each suggestion factor on the Cook page has an on/off checkbox; an unchecked factor is dropped from the request.
- **Stream Deck weather and forecast keys cycle.** Pressing the weather key cycles through stats and the forecast key cycles through days, each returning to its default after a short idle.
- **AI Declarations moved.** The standalone AI Declarations page is gone; the same content now lives in a section of the About page and in `docs/AI_DECLARATIONS.md`.
- **Cook icon unified.** Cook uses a flame icon consistently across the web UI and the Stream Deck.
- **Pi setup matches the board.** On a low-RAM or older Raspberry Pi (a Pi 3, Zero, or under about 4 GB of RAM), the setup wizard now offers Pi Remote only and hides Pi Hosted, since a full local stack (Grocy plus optional Mealie and Ollama) needs more than those boards have. A capable Pi 4 or 5 still offers both, and uncertain detection never over-restricts a box.

### Security
- **Web-UI password and kiosk PIN hashed at rest.** The login password and kiosk PIN are now stored as salted scrypt hashes instead of plaintext, so a leaked settings.json or backup does not expose them. Existing plaintext values still work and are upgraded to a hash on the next successful login. API keys and the TOTP secret stay as they are, since they are bearer secrets that must be presented verbatim.
- **Community health and supply-chain hardening.** Added SECURITY.md (a private vulnerability disclosure policy), CONTRIBUTING.md, CODE_OF_CONDUCT.md, issue and pull request templates, a Dependabot config (pip, GitHub Actions, Docker), a pre-commit config running ruff, test-coverage reporting in CI, and pinned every GitHub Actions reference to a commit SHA.

### Build
- **Hash-locked dependency file for reproducible builds.** A new `service/requirements.lock` resolves the full transitive dependency tree with `--generate-hashes`, so installs can be verified against known checksums. It also pins the previously floating `anthropic` dependency to a concrete version. The lockfile is additive: the Docker image still installs from `service/requirements.txt`. The README documents how to regenerate it with uv or pip-tools.
- **MkDocs site over the existing docs.** A root `mkdocs.yml` wires the files under `docs/` into a browsable site with the Material theme and a nav. It does not change any documentation content. MkDocs and its theme are dev-only tools and are not added to the runtime requirements; preview locally with `mkdocs serve`.

## [0.6.0] - 2026-06-26

This is the first version under the project's pre-1.0 scheme. Earlier `1.x`
tags were retired: everything to date is pre-launch, and `1.0.0` is reserved
for the public release. See the note at the bottom of this file.

### Added
- **On-device Pi installer.** A new `install.sh` (run on the device over SSH: `curl -fsSL .../install.sh | bash`) replaces the old "edit config on your PC and copy a payload onto the boot partition" flow. Flash a stock Raspberry Pi OS Lite card with Imager, boot, SSH in, and run one line. The installer detects the board and attached hardware, asks only for the deployment mode (Pi Hosted or Pi Remote), then hands off to the web setup wizard for all further configuration. Pi Remote installs nothing heavy. Supports unattended use with `NONINTERACTIVE=1` plus env vars.
- **Web-based appliance configuration.** After the one-line SSH install, the terminal prints the `http://foodassistant.local:9284/setup` URL and exits. All remaining setup (password, Grocy API key, AI provider, display orientation, Stream Deck, Mealie, etc.) happens in the browser. This makes pre-ship configuration possible: configure before shipping, customer plugs in and opens the URL.
- **Appliance settings panes (Pi only).** Three new sections appear in Settings when running on a Raspberry Pi: **Display** (kiosk scale, CSS rotation, and KMS framebuffer rotation with optional immediate reboot), **Stream Deck** (enable/disable, model selection, service restart), and **Network** (current Wi-Fi SSID, connect to a new network, change hostname). All accessible at any time after first setup.
- **Host bridge service.** A small Python helper (`foodassistant-host-bridge`, installed by `firstboot.sh` at `/usr/local/bin/`) runs on the Pi host at `127.0.0.1:9299`. It lets the Docker container call host-level operations (Wi-Fi via `nmcli`, hostname via `hostnamectl`, KMS rotation via `foodassistant-set-rotation`, Stream Deck service restart via `systemctl`) without running privileged inside Docker. Reachable from the container because `docker-compose.appliance.yml` uses `network_mode: host`.
- **Hardware settings pane.** Barcode scanner configuration moved out of the Inventory and Interface panes into a dedicated Hardware section, visible on all devices, with a global-capture switch and Waveshare scanner setup link.
- **Navbar health warnings (Pi).** The navbar surfaces a warning icon when the Pi reports undervoltage, throttling, high temperature, or low disk, read from the host bridge. The tooltip lists the active warnings.
- **Stream Deck size auto-detect.** Setup detects the attached deck (6/15/32 keys) and prefills the model, with a hint when it was filled from the hardware.
- **Stream Deck weather sync.** A satellite's deck mirrors the main server's weather location and units automatically, so the widget matches without separate local setup.
- **Stream Deck themed keys.** Key colors follow the active web UI theme (light, darkly, cyborg, flatly, synthwave); the default dark theme keeps the existing per-action colors.
- **Named Stream Deck profiles.** Save key layouts as named profiles on the main server, each targeting a deck size (6/15/32). A profile picker in the Stream Deck settings filters to the current deck, and satellites mirror the profile list on sync.
- **Deployment modes in setup.** The first setup step now asks how the device is used. On a Raspberry Pi you choose **Pi Hosted** (everything runs on the Pi, with or without a screen) or **Pi Remote** (a thin control surface that drives a Stream Deck and/or kiosk pointed at a Pantry Raider server already running elsewhere); on other hardware it stays **Server hosted**. Pi Remote installs no local Grocy or Docker, so it runs on a Pi 3, and the wizard skips the Grocy and AI steps for it. The choice is detected and offered automatically based on the board.
- **Shopping list without Mealie.** The Shopping tab is now always visible. When Mealie is not configured it is backed by Grocy's built-in shopping list: add, check off, and delete items. Multi-list selector appears when more than one list exists. A "Clear checked" button removes done items. When Mealie is configured the existing Mealie-backed view is unchanged.
- **Stock journal.** A new Stock Journal page (link in the Inventory header) shows the last 50/100/200 stock transactions from Grocy: date, product name, transaction type (Added, Consumed, Moved, Corrected), quantity, and note. A live text filter narrows by product name.
- **KMS display rotation.** Set `DISPLAY_ROTATION=90` (or 180, 270) in `image/config.env` before flashing to rotate the framebuffer at the OS level. Rotates the boot console and kiosk browser, unlike the CSS-only setting in the app. A `foodassistant-set-rotation` helper script is installed for runtime changes without reflashing.
- **Barcode scanner type** selector in the setup wizard (USB HID or Camera). USB HID includes a test input to confirm the scanner sends Enter after each code.
- **Settings status indicators.** The Settings sidebar shows colored icons for each section so misconfigured areas are visible at a glance. A warning banner appears at the top of Settings when Grocy is unreachable or no password is set.
- **Nav unlock hints.** When Mealie or another optional service is not configured, a small lock icon appears in the navbar with a tooltip listing the locked tabs and a link to the relevant Settings pane.
- **Stream Deck timers.** Three independent countdown timer keys (`timer_1`, `timer_2`, `timer_3`). Press to cycle through 5, 10, 15, 30, and 60 minute presets; press again to cancel. The key shows MM:SS while counting down, turns amber under 1 minute, and flashes red with "Done!" when the timer expires. Press once more to dismiss.
- **Targeted provisioner re-runs.** `STEPS=rotation,kiosk bash firstboot.sh` re-runs only the named steps, bypassing the done-marker check. Valid step names: `hostname`, `timezone`, `mdns`, `docker`, `stack`, `rotation`, `kiosk`, `streamdeck`.

### Changed
- **SD-card guide rewritten** around the SSH installer; nothing to edit on your PC and no repo clone on your PC. The pre-built turnkey image remains documented as an advanced no-SSH alternative.
- **Installer is now minimal at the terminal.** `install.sh` asks one question (deployment mode) and auto-detects kiosk/Stream Deck from attached hardware. Display rotation, Mealie, Ollama, and other add-ons are configured via the web UI after the install completes, not in the terminal.
- **Kiosk mode auto-hides reference pages** (phone QR, Defaults, About, AI Declarations, API docs, keyboard shortcuts) from the navbar so the touchscreen surface stays focused.

### Removed
- `scripts/image-build/prepare-image.ps1` and the documented boot-partition payload flow it implemented. Use the on-device installer instead. The pre-built image pipeline (`prepare-image.sh --image`, used by CI) is unchanged.

### Fixed
- QR modal now closes reliably on dark themes (modal was nested inside the collapsible navbar, causing backdrop conflicts).
- Grocy URL in the setup wizard now uses the browser's host instead of `localhost` when viewed from a different machine on the network.
- Navigation unlock hints show all tab names for a locked service, not just the last one.

## [0.5.0]

### Added
- **Ready-to-flash appliance image.** A prebuilt Raspberry Pi OS Lite image with the Pantry Raider provisioner baked in is published to the Releases page. Flash it with Raspberry Pi Imager, set wifi in the Imager GUI, and boot: no config files, no terminal. The device auto-detects an attached display (launches the kiosk) and a plugged-in Stream Deck, and takes its timezone from the OS.
- **Stream Deck controller** (`streamdeck/`). Drive an Elgato Stream Deck or embedded Stream Deck Module (6, 15, or 32 keys) as a physical control surface. Keys show live counts (items expiring soon, scans waiting to commit) and trigger actions like committing pending scans or steering the attached kiosk browser. Key text is large and legible, scales to each deck's pixel density, and the deck can be rotated 0/90/180/270 degrees. Includes a systemd unit, udev rule, and example config. See `streamdeck/README.md`.
- **Optional AI.** Pantry Raider now works without an AI provider. Inventory, expiry tracking, manual entry, and barcode lookup via Open Food Facts all keep working; photo import, receipt scanning, and recipe suggestions are simply off until you add a provider. Choose "None" in the setup wizard or **Settings → AI**.
- **Guided setup wizard.** First-time setup is now a step-by-step flow (welcome, security, Grocy, AI, optional integrations, done) with clear required fields, instead of one dense form.
- **Attached-display settings.** A scale (Small to Extra large) and orientation (0/90/180/270) control for a hardware screen wired to the appliance. These apply only to the kiosk display, never to a phone or laptop browsing the app.
- **About & Credits page** listing the open-source projects Pantry Raider builds on (Grocy, Mealie, Open Food Facts, TheMealDB, and more), with links and a note to support them.

### Changed
- Default Gemini model is now `gemini-2.5-flash` (the old `gemini-1.5-flash` default is no longer available).
- README rewritten with a "Why Pantry Raider?" section; the full API reference moved to `docs/api.md`.

### Fixed
- **Theme and display scale save immediately.** Picking a theme or scale in **Settings → Interface** applies and persists right away instead of waiting for **Save All**.
- QR code is now scannable on dark themes (white background), and the QR modal closes reliably.
- The appliance first boot no longer clobbers Raspberry Pi Imager's wifi/SSH/user setup (our provisioner script was renamed to avoid the collision).

## [0.4.0]

### Added
- **Remote Access**: expose your Pantry Raider to the internet without port-forwarding. In **Settings → Remote Access**, choose Cloudflare Tunnel (free, bring your own token) or Forager (managed subscription, coming soon). The tunnel runs as a sidecar container; your public URL appears in the UI once the connection is established.
- **Phone QR code**: a QR icon in the navbar opens a modal with a scannable code that jumps your phone's browser directly to the add-item page. Use your phone's camera for food photos without typing the server address.
- **Kiosk / touch mode**: a tablet icon in the navbar toggles touch-optimised sizing (48 px minimum tap targets on buttons, inputs, and list items). Useful on a countertop touchscreen; the preference is remembered in the browser.
- **SD-card image tooling**: `scripts/image-build/` and `image/config.env` for building a flashable Raspberry Pi appliance image. First boot auto-installs Docker, starts the full stack, configures mDNS (`foodassistant.local`), and optionally launches a Chromium kiosk. Supports Pi 4, Pi 5, and generic ARM64/x86-64 Linux. See `docs/hardware/sd-image.md`.
- **Supported hardware guide**: `docs/hardware/supported-hardware.md` lists tested boards, minimum RAM requirements, and peripheral compatibility (barcode scanners, displays, cameras).

### Changed
- **Cook page preference panel**: complexity, spice, max-cook-time, portions sliders and dietary-preference pills (Vegetarian, Vegan, Keto, Gluten Free, etc.) now also filter web recipe suggestions (Spoonacular via `complexSearch`), not just AI suggestions. Cuisine picker (Asian, Italian, Thai, Mexican, and more) added with broad-region expansion for TheMealDB.
- AI-only preference hints added alongside sliders that cannot filter recipe databases (Complexity, Spice, Portions).

### Fixed
- TheMealDB dietary post-filter now correctly blocks compound ingredient names (e.g. "parmesan cheese" catches the vegan exclusion for "cheese").

## [0.3.1]

### Added
- **Synthwave theme**: a new neon-on-dark theme (hot pink, electric cyan, purple) with glow accents, in **Settings → Interface**.

### Fixed
- Corrected badge text contrast in the Cyborg and Darkly themes, where status labels (Today, Refrigerated, etc.) could be hard to read.

## [0.3.0]

### Added
- Theme switcher in **Settings → Interface**: choose between Dark and Light (and extra built-in themes), applied across the whole app.

### Changed
- Reorganized the Settings menu into clearer sections. Storage categories now live under **Inventory**, recipe-suggestion tuning under **Recipes**, and backup/update tools under a dedicated **Backup & Updates** section.
- "What can I cook?" now matches your stock against the external recipe database (TheMealDB) much more reliably, so web recipe ideas show up alongside your own Mealie recipes.

### Fixed
- Corrected a Settings toggle that could fail to update its hint text.

## [0.2.0]

### Added
- **Grocy public URL**: set a separate external address for Grocy so the in-app links work through a reverse proxy while internal API calls stay on the local network.
- **Auto-check shopping list**: optionally tick items off your Mealie shopping list automatically when you scan and commit a matching item.

### Fixed
- The app no longer fails to start when its data directory is read-only on first launch.
- Corrected a Home Assistant automation sensor reference so the "expiring in 3 days" alert fires reliably.

## [0.1.0]

### Added
- **Custom storage locations**: define your own storage buckets beyond the four built-ins (Refrigerated, Frozen, Room Temp, Pantry), such as Wine Cellar or Garage Fridge.
- Screenshots and an expanded setup guide in the README.

### Changed
- Pinned the bundled Grocy, Mealie, and Ollama images to specific versions so an unattended update can't move you onto a breaking release. Documented how to upgrade them safely.

## [0.0.1]

First working build (the original pre-launch baseline).

### Added
- **Inventory dashboard** with storage panels, drag-and-drop moves, inline edits, and expiry badges, backed by Grocy.
- **Photo analysis**: photograph a food item to extract name, brand, quantity, and printed best-by date.
- **Receipt import**: photograph a grocery receipt to queue every food line item for review.
- **Barcode lookup** via camera, USB/wireless scanner, or manual entry, backed by Open Food Facts with optional AI name cleanup.
- **Expiry defaults**: an editable rules table that fills in best-by dates by product type.
- **Recipe suggestions** ("What can I cook?") ranked by what you already have in stock, with items expiring soon floated to the top.
- **Recipe import** from a webpage, a photographed recipe card, TheMealDB, or AI-generated from a dish name.
- **Meal planning and shopping lists** through optional Mealie integration, including a week view and check-off shopping list.
- **Home Assistant integration**: REST sensors, notification automations, and a Lovelace dashboard.
- **Web setup wizard** with live connection tests.
- **Two-factor authentication** (TOTP) on top of password login.
- **Backups**: download your data as a zip, with optional scheduled off-box backup via rclone.
- Optional fully-local operation using Ollama for vision and text.
- Docker, Docker Compose, and Home Assistant add-on installation paths.

---

## A note on versioning

This project was briefly tagged `1.0.0` through `1.5.0` during early
development before it had any real users. Those tags were retired and the
history re-anchored under a pre-1.0 scheme: the versions above are all
pre-launch milestones, and `1.0.0` is reserved for the first public release.
The mapping from the old tags was a straight subtract-one-major (old `1.6.0`
became `0.6.0`), with the genesis release floored to `0.0.1`.
