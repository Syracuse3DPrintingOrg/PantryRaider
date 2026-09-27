# Using your phone's camera

The phone in your pocket is the best camera in the kitchen. Pantry Raider uses
it the simple way: you open the app in your phone's browser and point the
camera at things. There is no app to install and nothing to pair.

## Getting the app onto your phone

You do not have to type an address. Anywhere in the app, tap the **phone QR
code** button in the navigation bar, or **Open on phone** on the Manage Pantry
page. A code appears with the caption "Scan with your phone to add items".
Point your phone's camera at it and follow the link.

The code carries your kitchen's address on your own network, so your phone
needs to be on the same wifi. If you have
[remote access](remote-access.md) turned on, you can set the code to hand out
your public address instead, and then it works from anywhere. That switch is in
Settings, under the QR code options.

The page it opens is the same Manage Pantry page you see on a computer, laid
out for a phone. Along the top are the four things a scan can do: **Stock up**,
**Use stock**, **Shopping list**, and **Audit**. Picking one here changes it
everywhere, including the counter scanner and the kitchen display, so everyone
in the house stays in step.

## Scanning barcodes

Under **Stock up** (or any of the other modes), the **Barcode** tab gives you
two ways to scan.

**Start Camera** opens a live view: hold a barcode in the frame and it reads it
as soon as it focuses. It handles the usual grocery codes (EAN-13, EAN-8,
UPC-A, UPC-E) plus Code 128, Code 39, and QR codes. Phones cannot focus much
closer than about four inches, so hold the package a little further back than
feels natural and let it settle.

**Snap Barcode Photo** takes a single picture with your normal camera app and
reads the code from that. It is slower by a beat, but it always works, and it
is the one to use if Start Camera is not offered (see below).

There is also a box to type a barcode by hand when the label is scuffed.

Every scan looks the product up in the community Open Food Facts database to
turn the number into a name. That lookup needs the internet; the scan itself
does not. If you are offline, type the name in and everything else works the
same.

## Photographing groceries and receipts

With an AI provider set up, the **Photo / Receipt** tab is where the phone
really earns its place.

- **Food.** Photograph a pile of groceries on the counter and every item in the
  shot is queued for you to review and confirm, names, brands, quantities, and
  any printed best-by date included. One photo beats twenty rows of typing.
- **Receipt.** Photograph the receipt and the items come off it the same way.

Reading a photo takes a minute or two, so you are free to put the phone down
and walk away. When it finishes, the results are waiting for you, with a note
in your review inbox and a quick message on the kitchen display.

Two more places take a photo straight from your phone:

- **Shopping, Add receipt** matches the prices you actually paid to the items
  in your pantry, so Grocy's price history reflects real money.
- **Recipes, import from a photo** reads a recipe off a cookbook page or a
  handwritten card.

If you have not set up an AI provider, these tabs are hidden rather than
offered and broken. Barcode scanning and manual entry work regardless.

## If Start Camera is missing

Browsers only hand out live camera access to pages served over HTTPS (or
running on the same machine). On a plain `http://` address on your own network,
your phone's browser will not allow a live camera view, so the app hides
**Start Camera** and shows a note pointing you at **Snap Barcode Photo**
instead.

That fallback is not a downgrade in what you can do. It uses your phone's
normal camera app, which is allowed over plain HTTP, and it reads the same
codes. Photo and receipt import work over plain HTTP too, for the same reason.

If you want the live view, give the app an HTTPS address. There are three ways:

- Turn on [remote access](remote-access.md), which publishes your kitchen over
  HTTPS with a real certificate and no router setup.
- Put a reverse proxy in front of it (Caddy, nginx, Traefik, Pangolin) and let
  it handle certificates. See the HTTPS section of
  [Platforms and deployment](platforms.md).
- Use a self-signed certificate on your LAN. It works, but every browser in the
  house will warn about it once.

## On the kitchen display

A kiosk screen hides the camera controls on purpose. A wall-mounted panel has
no useful camera, and a countertop barcode reader is faster than any phone. The
page says so, and offers **Open on phone** so you can hand the job to the
device that is actually good at it.
