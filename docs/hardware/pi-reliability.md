# Pi reliability and memory tiers

A Raspberry Pi runs Pantry Raider off an SD card, and SD cards have two failure
modes worth planning around: they wear out from constant small writes, and they
can corrupt if power is cut in the middle of a write. This page covers what a
fresh install does to protect the card, how to size the stack to your board's
RAM, and the exact steps to bring an already-running Pi up to these protections.

This page is about the Raspberry Pi appliance and satellite modes. A server
install on a mini PC or NAS (including an Unraid box) is not on an SD card and
is not affected by the card-specific measures here.

## Start with the right card

The cheapest, highest-value thing you can do is buy the right card. **Use a
high-endurance or industrial microSD card** (Samsung PRO Endurance, SanDisk High
Endurance, or an industrial-grade card), not an ordinary consumer card. These
are rated for continuous writing, which is exactly what a full host does: Grocy
and Mealie are databases that write to the card all day. An ordinary card is not
rated for that at all and wears out far sooner. A high-endurance card costs only
a little more and is worth it on any always-on Pi.

**This matters most on a full host (pi_hosted).** A thin remote (pi_remote)
barely writes, so a good ordinary card is fine there; a full host running the
databases is where a high-endurance card earns its keep.

One thing that is **not** a fix: swapping the SD card for a cheap USB thumb
drive. A budget thumb drive uses the same cheap memory with a weaker controller
and is often less reliable than a good SD card. If you want a real durability
upgrade beyond a high-endurance card, that is a USB SSD (a proper solid-state
drive over USB, not a thumb drive), which is a larger change planned for later.

## What a fresh install already does

Flash a card and install as usual (see the [SD-card image guide](sd-image.md))
and the provisioner applies all of the following on a Raspberry Pi. Each is
reversible, and each is described so you know what changed and why.

### Fewer writes to the card

- **Logs live in RAM.** The system journal is set to volatile storage, so
  routine logging goes to memory instead of writing to the card thousands of
  times a day. Logs reset on reboot, which is the right trade for an appliance:
  the card lasts far longer. The one-time install log under
  `/var/log/foodassistant-firstboot.log` still lands on disk so you can read it
  after a reboot.
- **No access-time writes.** The main filesystem mounts with `noatime`, so
  simply reading a file no longer triggers a write to update its timestamp.
- **Batched writes.** The filesystem is set to flush every two minutes
  (`commit=120`) rather than every few seconds. Fewer, larger writes are gentler
  on the card and shrink the window in which a power cut can catch a write in
  progress.

### No swap on the card

Swapping to the SD card is slow and one of the fastest ways to wear it out. A
fresh install disables the default on-card swap file and sets up **zram**
instead: compressed swap that lives in RAM. On a small board this buys real
headroom without ever touching the card. It is sized to half of your RAM and,
because it compresses roughly two to three times, gives more usable space than
that while staying in memory.

### Power-loss-safe updates

Over-the-air updates are ordered so a power cut can never leave a half-updated
system:

- The new app image is **pulled completely before anything switches over.**
  Docker only makes the new image the active one once every layer is present, so
  losing power mid-download leaves the old, working image in place.
- Once the new image is fully on the card, the running container is swapped to
  it in one atomic step, and all containers are set to restart on their own
  after a reboot. Lose power during the swap and the device comes back on either
  the old or the new image, never a broken mix.
- Pending writes are flushed to the card at both points (after the download and
  after the swap), and the small update bookkeeping files are written to a
  temporary file and renamed into place, so those too are all-or-nothing.

### Everyday data is already safe

Your settings and the app's live state (timers, the current recipe, scanner
mode, and so on) are written with a temporary-file-and-rename pattern, so a power
cut leaves either the old version or the new version of each file, never a
truncated one. Nothing extra is needed for this; it is how the app has always
saved its data.

## Memory tiers: which stack to run on your board

Pantry Raider is modular, so you run only what your board can carry. The
installer picks a sensible default from the RAM it detects, and you can always
change it later.

| Board RAM | Default stack | Add-ons |
|-----------|---------------|---------|
| 2 GB (Pi 4B 2 GB) | Pantry Raider + Grocy | Mealie is left off. Turn it on only if you find you have headroom. |
| 4 GB / 8 GB (Pi 4B, Pi 5) | Pantry Raider + Grocy + Mealie | Comfortable. This is the recommended appliance. |
| 1 GB or less (Pi 3B+, Pi Zero 2 W) | Satellite (thin remote) only | No local stack. Drives a kiosk or Stream Deck pointed at a bigger server. |
| 16 GB+ / x86-64 | Everything, including local AI | Local AI with Ollama wants this much RAM and is best on x86-64. On a Pi, use a cloud AI provider instead. |

**Why Mealie is off by default on a 2 GB board.** Pantry Raider and Grocy fit
comfortably in 2 GB, but adding Mealie (recipes, meal plan, shopping) pushes the
practical floor to 4 GB, and it is tightest exactly when you are meal planning.
The installer leaves it off there so a 2 GB device stays responsive. This is only
a default: if you ask for Mealie explicitly, you get it.

**Turning Mealie on later** on a board that can take it:

```bash
cd /opt/foodassistant
docker compose --profile with-mealie up -d
```

The Settings page offers the same toggle without the command line.

**Local AI (Ollama)** is not recommended on a Pi. Vision models are slow on an
SBC and want 16 GB or more, so on a Pi choose a cloud AI provider (Gemini,
OpenAI, or Anthropic) in the setup wizard. Run Ollama on an x86-64 machine with
plenty of RAM if you want fully local inference.

## GPU memory on a headless appliance

On the modern 64-bit Pi OS the graphics stack manages its own memory, so there
is no fixed GPU split to hand-tune the way older Pi guides describe. A kiosk
device leaves graphics memory alone so the touchscreen and browser have what they
need. A headless box (no display attached) simply never allocates it. There is
nothing to set here for either shape.

## Updating an existing Pi before the fleet gets these changes

The project is pre-launch, so a Pi you flashed earlier will not have the
card-protection measures until you update it. Pick whichever path fits.

You do not need to touch a server install (mini PC, NAS, Unraid). None of the
SD-card measures apply to it.

### Option A: re-run the installer (recommended, no reflash)

This keeps all your data and settings and just re-applies provisioning, which now
includes the new protections. SSH into the Pi and run:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

If the installer reports it is already provisioned and skips the work, force it
to re-run:

```bash
sudo rm -f /var/lib/foodassistant/firstboot.done
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

The card-protection steps (`sd_resilience` and `zram`) and the RAM-tiered Mealie
default are applied along with everything else. Reboot when it finishes so the
volatile journal, `noatime`, and `commit=120` take effect:

```bash
sudo reboot
```

### Option B: apply just the protections by hand

If you would rather not re-run the whole installer, apply the two new steps
directly. From the on-device checkout (usually `/opt/foodassistant-src` or
`/home/foodassistant/FoodAssistant`), pull the latest and run the targeted
provisioning steps:

```bash
cd /opt/foodassistant-src   # or wherever your checkout lives
git pull --ff-only
sudo STEPS=sd_resilience,zram bash scripts/image-build/firstboot.sh
sudo reboot
```

`STEPS=` runs only those two steps and nothing else, so it will not touch your
kiosk, Stream Deck, or any other setting.

### Option C: fresh reflash

A clean flash of the current image (or a stock card plus the installer) comes
with everything already in place. This is the simplest way to be certain a
device is fully current, at the cost of setting it up again. Back up first from
Settings, Backups, flash, then restore.

### Confirming it worked

After a reboot, a few quick checks:

```bash
# Journal is volatile (logs in RAM, not on the card):
journalctl --disk-usage        # should be tiny or "in the volatile store"

# Root filesystem mounts noatime:
findmnt / -o OPTIONS           # should list noatime and commit=120

# zram swap is active and on-card swap is gone:
swapon --show                  # should show /dev/zram0, not /var/swap
systemctl is-enabled dphys-swapfile   # should be disabled or masked
```

## Reversing any of it

Every change is reversible without a reflash:

- **Journal back to disk:** delete
  `/etc/systemd/journald.conf.d/foodassistant-volatile.conf` and reboot.
- **Undo the mount options:** a timestamped backup is left at
  `/etc/fstab.foodassistant.bak`. Remove `,noatime,commit=120` from the root line
  in `/etc/fstab` (or restore the backup) and reboot.
- **Turn off zram / restore on-card swap:** `sudo systemctl disable --now
  zramswap`, then re-enable `dphys-swapfile` if you want swap on the card again.

## A note on testing before the fleet

The card-protection steps change real system files (the journal config, the
filesystem mount options, and the swap setup) and are best proven on a spare card
first. The decision logic (which stack to run per RAM tier, and that the update
pulls fully before switching) is covered by the automated tests, but the actual
on-card behavior after a real power cut can only be confirmed on hardware. Try a
spare card, pull the plug a few times mid-update and mid-use, and confirm the
device always comes back up before rolling the change out widely.
