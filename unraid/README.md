# Unraid Community Applications template

This folder holds everything needed to install Pantry Raider on Unraid and to
submit it to Community Applications (CA).

- `pantryraider.xml` - the single-container CA template. Installs the Pantry
  Raider app and points it at a Grocy (and optional Mealie) you run separately.
- `docker-compose.yml` - a full-stack file for the Docker Compose Manager
  plugin. Runs Pantry Raider, Grocy, and optional Mealie together, with data
  under `/mnt/user/appdata`.

The user-facing install walkthrough lives in the docs at
[`docs/unraid.md`](../docs/unraid.md). This README is the reference for what the
template contains and how to submit it.

## The image

The template uses the project's published image, pinned by name:

    ghcr.io/syracuse3dprintingorg/pantryraider

The app listens on port **8000** inside the container. The template maps it to
host port **9284** (the project's standard Pantry Raider port), so the WebUI is
`http://YOUR-UNRAID-IP:9284/`.

## What the template exposes

Everything below is a real environment variable the app reads (from
`service/app/config.py` and `.env.example`) or a real port/path from the
project compose files. Nothing is invented; anything left blank in the template
can also be set later on the app's `/setup` page.

| Field (CA label) | Maps to | Kind | Source |
|---|---|---|---|
| WebUI Port | host 9284 -> container 8000 | Port | `docker-compose.yml` ports `9284:8000` |
| App Data | `/mnt/user/appdata/pantryraider` -> `/app/data` | Path | `config.py` `data_dir = "/app/data"` |
| Grocy Address | `GROCY_BASE_URL` | Variable | `config.py` `grocy_base_url` |
| Grocy API Key | `GROCY_API_KEY` (masked) | Variable | `config.py` `grocy_api_key` |
| Vision Provider | `VISION_PROVIDER` | Variable | `config.py` `vision_provider` |
| Gemini API Key | `GEMINI_API_KEY` (masked) | Variable | `config.py` `gemini_api_key` |
| UI Password | `AUTH_PASSWORD` (masked) | Variable | `config.py` `auth_password` |
| Mealie Address | `MEALIE_BASE_URL` | Variable | `config.py` `mealie_base_url` |
| Mealie API Key | `MEALIE_API_KEY` (masked) | Variable | `config.py` `mealie_api_key` |
| Require Login | `AUTH_REQUIRED` | Variable | `config.py` `auth_required` |
| API Key | `API_KEY` (masked) | Variable | `config.py` `api_key` |
| Deployment Mode | `DEPLOYMENT_MODE` | Variable | `config.py` `deployment_mode` |
| Timezone | `TZ` | Variable | project compose `environment: TZ` |
| Secret Key | `SECRET_KEY` (masked) | Variable | `config.py` `secret_key` |

The app reads env vars by the uppercased field name (pydantic-settings,
`model_config = SettingsConfigDict(env_file=".env", extra="ignore")`, no
prefix), so `GROCY_BASE_URL` sets `grocy_base_url`, and so on.

The icon is the project's pink raccoon mark, served straight from the repo:

    https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/service/app/static/icons/icon-512.png

(the 512x512 app icon in `service/app/static/icons/icon-512.png`).

## Single container vs. full stack

- **Single container (`pantryraider.xml`)**: the primary CA route. Pantry Raider
  is one image, and Grocy and Mealie already have their own CA templates, so the
  cleanest fit for the Apps tab is to ship just the app and let users bring their
  own backends. Install Grocy (and optionally Mealie) from CA, then install this
  template and point it at them.
- **Full stack (`docker-compose.yml`)**: for users who want everything in one
  place. CA does not install a multi-container stack from a single template;
  the community path for a stack on Unraid is the Docker Compose Manager plugin.
  This compose file is tuned for Unraid: appdata volumes under `/mnt/user`,
  `PUID=99`/`PGID=100` for Grocy, and the same pinned Grocy/Mealie versions the
  project ships. Mealie is commented out by default.

## Submitting to Community Applications (for the project owner)

1. Keep `pantryraider.xml` in this repo. Its `TemplateURL` points at the raw
   GitHub copy so CA can keep the installed template current.
2. Submit the repository through the Community Applications submission portal at
   `https://ca.unraid.net/submit`, which is the current source of truth for
   submission requirements and the publishing workflow. The older, still-used
   route is to send the CA moderators a private message (or post in the CA
   support forum thread) with the repository URL; a moderator reviews and adds
   the feed, usually within a couple of hours.
3. CA runs automated validation (well-formed XML, no template pet-peeves,
   reachable icon). Problems surface under **Template Errors** / **Invalid
   Templates** in CA settings, and the app will not appear in the feed (about a
   2-hour cycle) until they are fixed.
4. Note: templates that pass into dockerMan are moderated, so CA may make small
   fixes (typos, deprecated fields) on top of what is submitted.

References used when building this template:

- Unraid Docs, Community Applications: https://docs.unraid.net/community-applications/
- Docker template XML schema (fields and `Config` types):
  https://selfhosters.net/docker/templating/templating/ and the Unraid forums
  "Docker Template XML Schema" thread.
- Publishing to CA:
  https://forums.unraid.net/topic/101424-how-to-publish-docker-templates-to-community-applications-on-unraid/

## Notes and open questions

- CA has no single-template mechanism for a multi-container stack, so the full
  appliance is offered through Compose Manager rather than as a CA app. This is
  the standard Unraid approach today; confirm the Compose Manager steps against
  the current plugin UI, which changes occasionally.
- The `Support` link points at the project's GitHub issues. If an Unraid support
  forum thread is created later, switch `Support` to that thread (CA prefers a
  forum support URL).
- The exact submission portal flow (`ca.unraid.net/submit`) may differ from the
  older moderator-PM route depending on when this is submitted; both are listed
  above so whichever is current will work.
