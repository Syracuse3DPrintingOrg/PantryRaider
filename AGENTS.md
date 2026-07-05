# Pantry Raider: Agent Instructions

Canonical instructions for every AI agent working in this repo (Claude Code,
Codex, and anything else). `CLAUDE.md` is just a pointer to this file; edit
here, not there.

> **ALWAYS USE BEADS, AND THE WORK IS NOT DONE UNTIL IT IS COMMITTED AND
> PUSHED.** This repo tracks ALL work in **bd (beads)**, never in markdown
> TODOs, TodoWrite, or ad-hoc lists. Start every session with `bd prime`,
> pick work with `bd ready`, claim it (`bd update <id> --claim`), and close
> it when done (`bd close <id>`). File a new bead for ANY follow-up work
> you discover. Closing a bead means the change is tested, committed on
> `main`, and pushed to GitHub; never end a beads session with unpushed
> work. Phase 0 (FoodAssistant-7cc) holds discussion items that gate later
> phases; don't start blocked work, surface the blocking decision to Dan
> instead.

## What This Is

Self-hosted food spoilage tracker: a FastAPI service (port 9284) backed by
**Grocy** (inventory, port 9383), with optional **Mealie** (recipes, meal
plan, shopping; port 9285) and **Ollama** (local LLM), all run via Docker
Compose profiles (`--profile with-grocy / with-mealie / with-ollama`).

## Deployment Modes and Fleet Terms

These terms appear throughout the code and beads; `install.sh` prompts for
the mode and `image/config.env` pre-seeds it on a flashed image.

- **server**: the plain Docker Compose stack on any Debian/Ubuntu box (the
  default off-Pi).
- **pi_hosted**: full stack on a Raspberry Pi appliance (Pantry Raider +
  Grocy, optional Mealie), usually with the local kiosk display and/or a
  Stream Deck.
- **pi_remote** (a "satellite"): thin client, no Docker/Grocy/Mealie on the
  device, just a kiosk and/or Stream Deck pointed at a server elsewhere. It
  pulls backend config from the main server (`services/satellite.py`) and
  appears in the server's device registry (`services/devices.py`).
- **host-bridge**: `foodassistant-host-bridge`, the root helper on a Pi at
  `127.0.0.1:9299` that performs OTA updates, restores, and reboots on
  behalf of the containerized app.

## Brand and Identifiers

- Brand: **Pantry Raider** (`APP_NAME`, pink `#F2006E` raccoon). Owner org:
  **Syracuse3DPrintingOrg**; published image
  `ghcr.io/syracuse3dprintingorg/pantryraider` (publish-image.yml PINS the
  image name; never derive it from `github.repository`).
- Device-local identifiers intentionally stay **foodassistant** and must
  NOT be renamed: systemd units, `/opt` and `/etc` paths,
  `foodassistant_streamdeck`, `foodassistant.local`, the AP SSID, the
  `/health` `app=foodassistant` field, and the `FoodAssistant-*` beads
  prefix. Renaming any of them breaks deployed devices; do not "finish the
  rebrand".
- Amazon affiliate links use Dan's static tag `improvisedeng-20` and
  storefront `https://www.amazon.com/shop/improvisedeng`
  (`AMAZON_ASSOCIATES_TAG` / `AMAZON_STOREFRONT_URL` in
  `service/app/config.py`, env-overridable). The tag is deliberately NOT a
  user setting: the point is affiliate revenue for the project owner.

## Writing Style

Applies to ALL project content: code comments, docs, README, CHANGELOG,
commit messages, and UI copy.

- No em-dashes. Use commas, parentheses, colons, or rewrite the sentence.
- No ASCII line or box diagrams.
- The goal is copy that reads as human-written; avoid LLM tells generally.
- Docs and UI copy are **user-forward**: written for the app's end user, not
  as notes to Dan or the developer. No option-weighing that reads like an
  agent asking for feedback, and no copy that describes the software from
  the builder's side ("Update now pulls the new image" style). Dan has had
  to flag this repeatedly; check for it before shipping any doc or UI text.

## Related Repositories

- **PantryRaiderWeb**: the pantryraider.app website (the interactive demo
  source lives here under `docs/demo` and deploys to Cloudflare).
- A **private hardware repo** holds the Stream Deck Module physical mount
  and CAD work (see FoodAssistant-6l0); CAD does not belong in this repo.

## Repository Map

| Path | What lives there |
|---|---|
| `service/` | The FastAPI app (routers, services, providers, templates, static JS) |
| `streamdeck/` | `foodassistant_streamdeck`, the physical Stream Deck controller (systemd + udev units included) |
| `homeassistant/` | REST sensor config, automations (barcode scanner via keyboard_remote), Lovelace dashboard |
| `scripts/` | Backup/restore, version bump, git hooks, screenshot capture, and `image-build/` Pi provisioning |
| `dashboard/` | Cloudflare Worker (wrangler) |
| `docs/` | MkDocs content, hardware notes, the interactive browser demo (`docs/demo`) |
| `tests/` | Pure-logic pytest suite (no network or Docker needed) |
| `install.sh` | On-device installer loader for a Pi or Debian/Ubuntu box |

## Service Architecture

### Core

- `service/app/main.py`: FastAPI app; middleware order matters
  (setup-redirect, then auth, then session).
- `service/app/config.py`: pydantic settings; env vars override
  `service/data/settings.json` (written by the `/setup` wizard); `_SAVEABLE`
  lists persistable keys. Also holds `APP_VERSION`.
- `service/app/providers/`: `VisionProvider` ABC (gemini / openai /
  anthropic / ollama), built by `dependencies._build_provider()`, cached with
  `lru_cache`, invalidated by `reset_providers()` on settings save.

### Services (`service/app/services/`)

| Module | Purpose |
|---|---|
| `grocy.py` | Inventory client, plus `consume_by_barcode` |
| `mealie.py` | Recipes / meal plan / shopping client, plus the `suggest_recipes` inventory matcher |
| `barcode.py` | Open Food Facts lookup with LLM enrichment |
| `defaults.py` | Expiry rules |
| `current_recipe.py` | Active recipe (and courses), shared across workers via an mtime-checked state file under data_dir, plus the Mealie-detail normalizer |
| `timers.py` | Shared server-side timer registry, persisted to a state file under data_dir; countdowns derive from epoch deadlines so every worker and surface agrees |
| `recipe_timers.py` | Parses step durations into timer suggestions |
| `recipes_import.py` | Parse a recipe file: generic JSON, schema.org JSON-LD, or Mealie export |
| `recipes_external.py` | TheMealDB / Spoonacular suggestions and ingredient matching |
| `cameras.py` | Resolve a configured camera to a fetchable feed; HA cameras need a bearer header (not a token in the URL), so the app proxies them |
| `camera_scan.py` | IP-camera probe |
| `scanner_mode.py` | Barcode scanner mode (inventory / consume / shopping / audit), shared across workers via a state file under data_dir |
| `audit.py` | Location-scoped, read-only pantry stock count (scans compared to Grocy stock, never written back), shared across workers via a state file under data_dir |
| `nutrition.py` | Food-intake log totals; pairs with the `IntakeLog` model |
| `weather.py` | Kiosk forecast: Open-Meteo primary, wttr.in fallback; pure parse helpers |
| `satellite.py` | pi_remote pulls and persists backend config from the main server |
| `devices.py` | Main-server registry of satellite remotes, with an up-to-date/behind version badge via `version_compare.py` |
| `ha_events.py` | Ring of on-screen HA events (notification toasts, camera pop-ups), polled by the kiosk, shared across workers via a state file under data_dir |
| `diagnostics.py` | Debug logging to a rotating file under `data_dir/logs`, with redacted download |
| `auto_update.py` | Fleet-wide auto-update decision: a Pi appliance applies via the host-bridge OTA, a server via Watchtower, a satellite follows the server's flag |
| `utensils.py` | Recipe-equipment match |
| `action_items.py` | Action inbox |
| `lan_scan.py` | LAN instance sweep |

### Routers and UI (`service/app/routers/`, `templates/`, `static/js/`)

- REST and UI routes; templates are Bootstrap 5 dark-theme Jinja2.
- `current_recipe.py` serves the Current Recipe page (nav label "On the
  Line") and the `/timers` API.
- `admin.py` has backup plus `POST /admin/restore` (app data, zip-slip
  guarded, secrets preserved when a field is left blank).
- `ui.py` serves the kiosk pages: `/ui/camera` (plus the
  `/ui/camera/{i}/snapshot|stream` HA proxy and `/ui/camera/diag`),
  `/ui/weather` (plus `/ui/weather/data`), `/ui/convert`,
  `/ui/kitchen-guide`, `/ui/timers`, `/ui/audit`, `/ui/nutrition`,
  `/ui/journal`.
- `audit.py` (`/audit/*`, satellite-forwarding read-only stock count) and
  `nutrition.py` (`/nutrition/*` intake log) back those pages.
- `pending.py` has `/pending/scan` (routes by scanner mode) and
  `/pending/scanner-mode` (plus `/cycle`).
- `events.py` is the HA event channel (`POST /events/notify`,
  `POST /events/camera-popup`, `GET /events/poll`), rendered on screen by
  `static/js/ha-events.js` when `ha_events_enabled`.
- Browser-facing static JS lives in `static/js/`: `floating-nav.js`,
  `timer-chips.js`, `kiosk-idle.js`, `kiosk-auto.js`, `ha-events.js`.

### Pi provisioning (`scripts/image-build/`)

- `firstboot.sh` plus the `foodassistant-host-bridge` (host root helper at
  `127.0.0.1:9299`).
- OTA/restore helpers: `foodassistant-update` (redeploys app and Stream
  Deck, re-runnable after a manual pull) and `foodassistant-restore` (full
  Grocy+Mealie snapshot restore from a path or `rclone:` source, driven by
  the bridge `POST /restore`).

## Conventions and Gotchas

- **HA sensors use the LAN URL** (`http://192.168.1.170:9284`), never the
  Pangolin public URL (headless requests get an HTML redirect). Lovelace
  buttons use the public URL.
- App code is volume-mounted with `--reload`: `git pull` applies changes
  live; rebuild only for `requirements.txt` or Dockerfile changes.
- Mealie client auto-detects v1 (`/api/groups/`) vs v2 (`/api/households/`)
  API paths.
- HA template gotcha: compare `key_code` as integers (`key == 28`), never
  cast with `| string`.
- LLM JSON replies may be fenced; always parse with
  `providers.base.parse_json_response`.
- Cross-surface state (timers, current recipe, scanner mode, audit session,
  HA events) is **shared through small atomic JSON state files under
  data_dir** (temp file + `os.replace` writes, mtime-cached reads, silent
  in-memory degradation when data_dir is unwritable), so multiple uvicorn
  workers agree and the state survives a restart.
  `main.py` also heartbeats `data_dir/app-instance.json` and
  warns loudly at startup when another live process shares the data dir.
  Keep the normalization, scaling, and parse logic pure so it stays testable.
- Templates get an `ai_configured` flag from `templating.theme_context`;
  gate AI-only UI on it (`{% if ai_configured %}`) so the app never offers
  actions that cannot work without a provider.
- `/setup/save` applies only the fields present in the request
  (`model_dump(exclude_unset=True)`), so the per-section Save buttons post
  just their own fields without clobbering others.
- **Existing installs are production.** Dan's main server (Korolev, Unraid)
  is a live install that must survive every update; an early 0.7.x update
  wiped its Mealie data and theme. Settings and data changes must migrate
  what is already there, never reset or re-seed it.

## Documentation Pipeline

- Docs publish automatically on push to `main`: `wiki-sync.yml` mirrors
  `docs/` (plus README, CHANGELOG, etc.) to the GitHub wiki through
  `scripts/build-wiki.py`, and `docs-site.yml` builds the MkDocs site. The
  wiki is a generated mirror; never edit it directly, it gets overwritten
  on the next sync.
- Keep `docs/settings-matrix.md` current whenever a setting is added,
  moved, or changes visibility (it maps settings across server, pi_hosted,
  and pi_remote: which live where, which inherit from the server).

## Build and Test

```bash
docker compose up -d --build                 # run (add profiles as needed)

# local smoke test deps:
pip install fastapi jinja2 itsdangerous pillow python-multipart sqlalchemy pydantic-settings httpx "qrcode[pil]"
python -c "import sys; sys.path.insert(0,'service'); from app.main import app"

# tests (pure logic, no network or Docker needed):
pip install pytest && python -m pytest tests/ -q
```

Backups: `./scripts/backup.sh [dest]` (cron-friendly, 14-day rotation);
restore with `./scripts/restore.sh <archive>`.

**Definition of done:** before handing off a code change, run
`python -m pytest tests/ -q` (the suite is pure logic and cheap) and the
import smoke test above. A user-facing change also needs a CHANGELOG entry
(see Versioning).

## Versioning

- `APP_VERSION` in `service/app/config.py` is the single source of truth
  (major.minor.patch). The project is pre-1.0: `1.0.0` is reserved for the
  first public release, so stay in `0.x` until then.
- **Every commit changes at least the patch number.** Run
  `scripts/install-git-hooks.sh` once per clone to install a pre-commit hook
  that auto-bumps the patch; it chains onto the beads hook and skips rebases,
  merges, and beads-only commits. Re-run the installer if a beads hook update
  rewrites the managed hook.
- **Every user-facing change gets a CHANGELOG entry** under `[Unreleased]`
  in the appropriate Added/Changed/Fixed section, written in the existing
  plain-prose style (a bold one-line summary, then what it means for the
  user). The changelog doubles as the GitHub Release description, so write
  for users, not for developers.
- For a minor or major release, bump first so the hook stays out of the way:
  `scripts/bump-version.sh minor && git add service/app/config.py`. Move the
  CHANGELOG `[Unreleased]` items under the new version header and tag from
  `APP_VERSION` (`git tag -a v$(scripts/bump-version.sh --current)`).

## Authorship and Git

- **All commits are authored by Dan's GitHub identity**
  (`Syracuse3DPrinting <dm.marafino@gmail.com>`); the repo git config is
  already set, do not change it. Never add Co-Authored-By trailers, AI
  attributions, or session links to commit messages.
- Development happens on **`main`** directly.
- **Commit and push policy (unambiguous):** for beads work and any
  load-bearing change (code, docs, instructions, provisioning), commit and
  push are part of done. The deployed fleet updates from GitHub, so an
  unpushed change has not shipped. Do not wait to be asked.
- The one exception: small conversational tweaks Dan asks for while just
  chatting (a wording nit, a quick experiment) may be left uncommitted for
  his review, unless he says ship it. When in doubt, it is beads work:
  commit and push.
- GitHub interactions in cloud sessions go through the GitHub MCP
  integration (no `gh` CLI available there); `gh` may be used on local
  machines.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations. `cp`, `mv`, and
`rm` may be aliased to `-i` on some systems, which hangs an agent waiting
for y/n input. Use `cp -f`, `mv -f`, `rm -f` (and `-rf` for recursive
operations). Similarly: `scp`/`ssh` with `-o BatchMode=yes`, `apt-get -y`,
and `HOMEBREW_NO_AUTO_UPDATE=1` for `brew`.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

**This repository explicitly opts into the team-maintainer profile.** For
beads and other load-bearing work, agents close beads, run quality gates,
commit, and push as part of session close (see the commit and push policy
under Authorship and Git). The conservative profile below applies only to
the small-conversational-tweak exception, or when Dan currently says do
not commit or push.

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # This repo opts into team-maintainer: this is the DEFAULT close for
   # beads work, unless Dan currently says do not commit or push.
   git pull --rebase
   bd dolt push
   git push
   git status

   # Conversational-tweak exception only: report status and proposed
   # commands, wait for approval.
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
