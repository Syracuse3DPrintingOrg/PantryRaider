# FoodAssistant Appliance — V1 Hardware Spec

> **Status:** Planning / pre-prototype  
> **Last updated:** June 2026  
> **Scope:** Countertop/wall-mount appliance running the full local FoodAssistant stack with integrated touchscreen and barcode scanner. No embedded camera (users scan items; photos taken via phone browser). No local LLM in V1.

---

## SKU Lineup

| SKU | Display | Target retail |
|-----|---------|--------------|
| **Headless** | None — access via phone/tablet/browser on LAN | ~$249 |
| **7"** | 7" DSI capacitive touch, countertop or wall | ~$329 |
| **10"** | 10.1" DSI/HDMI capacitive touch, countertop or wall | ~$399 |

All three SKUs share identical compute and software. Retail targets are provisional and exposed to the 2026 RAM crisis (see below) — they assume current peak component pricing and should be revisited before any BOM commitment.

---

## ⚠️ The 2026 RAM Crisis Drives Everything

Spec this product with eyes open: **memory is the dominant cost variable right now, and it is at or near its worst.**

- DDR4/LPDDR4 prices have **doubled to quadrupled** since late 2025, driven by AI datacenter demand. A 32 GB DDR4 kit went from ~$70 (Oct 2025) to $150–260 (early 2026).
- The shortage is forecast to last **until at least Q4 2027**, with the **peak around mid-2026 — i.e. now.**
- This hits anything RAM-heavy hardest. The Beelink S12 Pro N100 with 16 GB sold for $169 in 2023; in mid-2026 it is **$430–497**. The Pi 5 4 GB bare board is ~$130 (>70% over MSRP).

**Strategic consequence — minimize RAM.** V1 has no local LLM, so the full stack fits in **4 GB** (FastAPI ~150 MB + Grocy ~200 MB + Mealie ~300 MB + OS). Buying 16 GB to sit idle is paying the crisis tax for nothing. The cheapest viable compute is therefore the *lowest-RAM* board that runs the stack — which puts the Pi 5 4 GB back in front of the N100, and brings its DSI display advantage back with it.

**Timing decision (open — see bottom of doc):** mid-2026 is the worst moment in years to lock BOM on a RAM-dependent appliance. Options are (a) design now, hold BOM commitment until RAM eases, (b) launch the software / headless-on-customer-hardware path first, ship physical units later, or (c) accept thin margins at current pricing. This needs an explicit call before prototyping.

---

## Compute: Raspberry Pi 5 (4 GB)

### Why Pi 5 over an N100 box (at current pricing)

The earlier draft of this doc recommended an N100 mini PC on the assumption it was ~$150–165. **That pricing is stale** — the RAM crisis has pushed 16 GB N100 boxes to $430+, making them the *worst* value, not the best. Re-evaluated:

| | Pi 5 4 GB | N100 mini PC, 16 GB (Beelink S12 Pro) | N100 mini PC, 8 GB |
|--|-----------|----------------------------------------|---------------------|
| Price (mid-2026) | **~$130** (bare board) | ~$430–497 (complete) | ~$200–250 (complete) |
| RAM | 4 GB (enough — no LLM) | 16 GB (wasted in V1) | 8 GB |
| Storage | add SD ~$12 | 500 GB NVMe incl. | incl. |
| Case / PSU | add ~$10 / ~$12 | incl. | incl. |
| Display interface | **DSI ribbon** (clean) | HDMI + USB touch | HDMI + USB touch |
| Architecture | ARM | x86-64 | x86-64 |
| Idle power | ~3–5 W | ~6–10 W | ~6–10 W |

Even after adding SD + PSU + case (~$165 all-in), the Pi 5 4 GB undercuts an 8 GB N100 box and is dramatically cheaper than the 16 GB unit — while giving the DSI display advantage. **Pi 5 4 GB is the V1 compute.**

### Recommended unit

**Raspberry Pi 5, 4 GB** + 64 GB A2 microSD + official 27 W USB-C PSU (listed/certified).
- DSI display connector (single ribbon to the panel — no USB touch controller)
- WiFi 6 / BT, Gigabit Ethernet, pre-certified radio (FCC/CE on the board)
- PCIe lane available for future NVMe/NPU (V2 hedge)

### x86 vs ARM note

Dropping x86 means losing the "first-class amd64 Docker image" convenience. In practice FoodAssistant, Grocy, and Mealie all ship working ARM64 images, so this is a non-issue for V1. The x86 path mainly mattered for future local-LLM headroom, which V1 explicitly defers — and the Pi 5's PCIe lane is the better V2 accelerator hedge anyway.

### Power

Pi 5 at idle (no LLM): ~3–5 W. Under load serving the web stack: ~8–12 W peak. Powered by the official listed 27 W USB-C PSU — certified, no custom power circuit. Safety cert burden stays on the PSU manufacturer.

---

## Display

### 7" SKU

**Waveshare 7" DSI Capacitive Touchscreen (C), 1024×600 IPS** (or equivalent)
- Single DSI ribbon to the Pi 5 — video + touch over one cable, no USB touch controller
- Capacitive 5-point touch, works with Chromium kiosk
- Toughened glass face panel, bezel-mountable
- Est. ~$45–60 (verify current pricing at purchase)

### 10" SKU

**Waveshare 10.1" DSI Capacitive Touchscreen, 1280×800 IPS** (or equivalent)
- DSI ribbon to the Pi 5 (HDMI+USB-touch variant is a fallback if a DSI panel at this size/res isn't available)
- Larger surface area for inventory grid, meal plan week view, recipe cards
- Est. ~$75–95 (verify current pricing at purchase)

### Display note — DSI is the win

With Pi 5 compute, both display SKUs use **DSI ribbon** rather than HDMI + USB touch: one cable, no separate touch controller, cleaner internal routing, smaller enclosure. This is the practical reason the RAM-crisis-forced switch back to Pi is a net positive for the display SKUs, not just a cost dodge.

---

## Barcode Scanner

**Waveshare Barcode Scanner Module** (1D/2D) or equivalent compact OEM scan engine
- USB HID — presents as a keyboard to the OS; zero driver work
- Reads 1D (UPC-A, EAN-13, Code 128, etc.) and 2D (QR, DataMatrix)
- Compact rectangular form factor; scan window mounts flush in bezel cutout
- Est. ~$20–35 (verify current pricing at purchase)

### Integration

The module sits in a dedicated bay in the FDM enclosure with its scan window exposed through a 30×20 mm cutout in the top bezel or right edge. USB pigtail routes internally to the Pi 5. From outside the device it looks built-in. Users wave items past the window; the decoded string fires into the focused browser tab as keystrokes, caught by the existing `barcode-input` listener on the add-item page.

No software changes needed — the existing barcode flow handles USB HID scanners already.

---

## Phone Camera (Photos & Receipts)

No embedded camera in V1. Users photograph items using their phone's browser:

1. Phone opens `http://foodassistant.local` (or IP) on the LAN — or the cloud subscription URL
2. `<input type="file" accept="image/*" capture="environment">` triggers native phone camera
3. Photo uploads to the existing `/analyze` endpoint

**Improvement needed (software task):** Add a QR code or scannable link on the appliance display that deep-links the phone directly to the upload page, eliminating the need to type the URL.

---

## Software Stack (all SKUs)

Identical Docker Compose stack on all units:

```
FoodAssistant (FastAPI, port 9284)
Grocy          (inventory backend, port 9383)
Mealie         (recipes/meal plan/shopping, port 9285)
```

AI: **cloud subscription only** in V1. No local LLM. Vision analysis routes to the cloud provider configured in settings (Gemini/OpenAI/Anthropic). This keeps thermals flat and the Pi 5 cool (passive heatsink, no fan).

**Kiosk UI (display SKUs):**
- Raspberry Pi OS Lite (64-bit)
- Cage (Wayland compositor) + Chromium `--kiosk --app=http://localhost:9284/ui/`
- On-screen keyboard: `wvkbd` or `squeekboard` for text input fields
- Auto-login, auto-start on boot

**Touch target sizing:** The current Bootstrap 5 UI is mouse/desktop-tuned. A software task is needed to increase tap targets and test the full workflow on a 7" 1024×600 screen before shipping display SKUs.

---

## Enclosure (Display SKUs)

### Material

**PETG** (not PLA) — kitchen counter proximity to heat sources causes PLA to creep/deform over time.

### Form factor

- Two-piece shell: base + face plate
- **VESA 75mm keyhole** on back for wall mount
- **Detachable angled foot/stand** (15° tilt toward user) for countertop — same enclosure shell, two deployments
- Expose through rear cutouts: USB-A (scanner + spare), Ethernet, USB-C PSU input, microSD access slot
- **Scanner bay** in top bezel or right edge with 30×20 mm window cutout
- Passive cooling: the Pi 5 without LLM stays cool enough at kitchen duty cycles with a heatsink; the official Pi 5 active cooler can be fitted in the heavier 10" SKU if thermal testing warrants

### Pi 5 mounting

The Pi 5 mounts to standoffs in the custom enclosure base (the well-known fixed Pi 5 board footprint makes this straightforward — no per-vendor dimension verification needed, unlike mini-PC boards). DSI ribbon runs from the board to the panel on the face plate.

---

## BOM Summary (estimates — verify at purchase; all RAM-crisis-exposed)

> Prices reflect mid-2026 peak component costs. The Pi 5 board (~$130) and microSD are the volatile lines — both should drop as the RAM shortage eases toward 2027–2028, improving every SKU's margin.

### Headless SKU (~$249 retail)

| Part | Est. cost |
|------|-----------|
| Raspberry Pi 5 4 GB | ~$130 |
| 64 GB A2 microSD | ~$12 |
| Official 27 W USB-C PSU (listed) | ~$12 |
| Pi 5 case + heatsink | ~$15 |
| Barcode scanner module (optional add-on) | ~$20–35 |
| **Compute total (no scanner)** | **~$169** |

Ships in an off-the-shelf Pi 5 case — minimal enclosure work. Scanner offered as a separate USB add-on users plug in themselves.

### 7" SKU (~$329 retail)

| Part | Est. cost |
|------|-----------|
| Raspberry Pi 5 4 GB | ~$130 |
| 64 GB A2 microSD | ~$12 |
| Official 27 W USB-C PSU (listed) | ~$12 |
| Waveshare 7" DSI touch panel | ~$45–60 |
| Barcode scanner module | ~$20–35 |
| Heatsink | ~$5 |
| PETG enclosure (FDM, in-house) | ~$12–16 |
| Misc (standoffs, DSI ribbon, internal cables) | ~$8–12 |
| **Total** | **~$244–282** |

### 10" SKU (~$399 retail)

| Part | Est. cost |
|------|-----------|
| Raspberry Pi 5 4 GB | ~$130 |
| 64 GB A2 microSD | ~$12 |
| Official 27 W USB-C PSU (listed) | ~$12 |
| Waveshare 10.1" DSI touch panel | ~$75–95 |
| Barcode scanner module | ~$20–35 |
| Active cooler (optional) | ~$5–10 |
| PETG enclosure (FDM, in-house, larger) | ~$16–22 |
| Misc | ~$8–12 |
| **Total** | **~$278–328** |

---

## Certification

- **FCC/CE radio cert:** Inherited from the Pi 5 (the on-board WiFi/BT module is pre-certified). No custom radio.
- **Safety:** Powered by the official listed Pi 5 USB-C PSU; internal power is just the board's regulators. No custom power circuit.
- **EMC (unintentional radiator):** For an assembled product sold as consumer electronics, a pre-scan at an EMC lab ($1–2K) is advisable before a production run. Pi 5 + DSI panel is a well-trodden combination; risk is low but worth checking. (DSI keeps high-speed signals on a short shielded ribbon rather than an external HDMI cable, which is mildly favorable for emissions vs. the HDMI approach.)
- **DIY kit path:** If sold as a kit (unassembled), FCC Part 15 Subpart B self-declaration + Declaration of Conformity. Cheaper, faster, standard for direct-sale products.

---

## Compute Watchlist & Alternatives

The compute choice is **RAM-price-driven and should be re-checked before any BOM commitment.** Triggers to re-evaluate:

- **N100 mini PC becomes viable again** if 8–16 GB box pricing falls back under ~$180 as RAM eases. x86 + included NVMe/case/PSU would again compete — but only the *display SKUs lose DSI* if you switch, so the bar to move is higher than pure price.
- **Pi 5 pricing normalizes** toward MSRP ($60 for 4 GB) — pure margin upside, no design change.
- **Cheaper low-RAM SBCs** (Radxa ROCK 3C / Orange Pi 3B, RK3566, 4 GB, ~$35–50) — viable fallback if Pi 5 supply tightens further; costs community/driver effort and the DSI ecosystem is weaker.

The software stack requires zero changes to switch compute platforms.

---

## Open Decision: Launch Timing vs. the RAM Crisis

**This needs an explicit call before prototyping.** Mid-2026 is the worst component-pricing window in years and it persists into 2027. Three paths:

1. **Design now, hold BOM commitment** until RAM eases (2027+). Finalize CAD, kiosk software, and the integration prototype on a single hand-built unit, but don't buy production quantities of boards at peak prices.
2. **Software / headless-first launch.** Ship the install-on-your-own-hardware path and the cloud subscription now (no inventory risk), and introduce the physical appliance SKUs once component costs recover. Lets the product earn revenue and validate demand before any hardware capital is committed.
3. **Launch at current pricing, accept thin margins.** Only if there's a strategic reason to have physical units in-market now that outweighs the ~$60–80/unit of crisis-inflated RAM/SD cost.

Recommendation leans toward **(2)** — it de-risks everything and matches where the software already is — but this is a business call for Dan, not a hardware one.

---

## Open Questions / Future SKUs

- [ ] **Decide launch timing** (see Open Decision above) before committing any BOM spend
- [ ] Test full FoodAssistant stack on Pi 5 / Pi OS Lite (ARM64 Docker images — verify Grocy + Mealie + FastAPI run clean in 4 GB)
- [ ] Test Chromium kiosk + wvkbd on Wayland at 1024×600 — verify UI tap target sizing
- [ ] Source a confirmed DSI panel for the 10" SKU (verify a 10.1" DSI option exists at target res; HDMI+USB-touch is the fallback)
- [ ] Phone QR code / deep link feature for photo upload UX
- [ ] **V2 consideration:** Use Pi 5 PCIe lane for NVMe and/or an LLM accelerator (HAT+ / M.2) when a viable LLM-capable accelerator exists
- [ ] **V2 consideration:** Camera module option (Pi CSI autofocus or USB) to enable on-device photo analysis without a phone
- [ ] **V2 consideration:** Revisit x86/N100 (and higher-RAM, local-LLM) builds once the RAM market recovers
