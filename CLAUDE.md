# Project Instructions for AI Agents

> **⚠️ ALWAYS USE BEADS.** This repo tracks ALL work in **bd (beads)** — never in
> markdown TODOs, TodoWrite, or ad-hoc lists. Start every session with `bd prime`,
> pick work with `bd ready`, claim it (`bd update <id> --claim`), and close it when
> done (`bd close <id>`). File a new bead for ANY follow-up work you discover.
> Phase 0 (FoodAssistant-7cc) holds discussion items that gate later phases —
> don't start blocked work; surface the blocking decision to Dan instead.

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

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

## Authorship & Git

- **All commits are authored by Dan** (`Dan <dm.marafino@gmail.com>`) — repo git
  config is already set. Never add Co-Authored-By trailers, AI attributions, or
  session links to commit messages.
- Development happens on **`main`** directly.
- GitHub interactions in cloud sessions go through the GitHub MCP integration
  (no `gh` CLI available there); `gh` may be used on local machines.

## Versioning

- `APP_VERSION` in `service/app/config.py` is the single source of truth
  (major.minor.patch). The project is pre-1.0: `1.0.0` is reserved for the
  first public release, so stay in `0.x` until then.
- **Every commit changes at least the patch number.** Run
  `scripts/install-git-hooks.sh` once per clone to install a pre-commit hook
  that auto-bumps the patch; it chains onto the beads hook and skips rebases,
  merges, and beads-only commits. Re-run the installer if a beads hook update
  rewrites the managed hook.
- For a minor or major release, bump first so the hook stays out of the way:
  `scripts/bump-version.sh minor && git add service/app/config.py`. Move the
  CHANGELOG `[Unreleased]` items under the new version header and tag from
  `APP_VERSION` (`git tag -a v$(scripts/bump-version.sh --current)`).

## What This Is

Self-hosted food spoilage tracker: FastAPI service (port 9284) backed by
**Grocy** (inventory, port 9383), with optional **Mealie** (recipes/meal
plan/shopping, port 9285) and **Ollama** (local LLM) — all via Docker Compose
profiles (`--profile with-grocy / with-mealie / with-ollama`).

## Architecture Overview

- `service/app/main.py` — FastAPI app; middleware order matters (setup-redirect → auth → session)
- `service/app/config.py` — pydantic settings; env vars override `service/data/settings.json` (written by the `/setup` wizard); `_SAVEABLE` lists persistable keys
- `service/app/providers/` — `VisionProvider` ABC (gemini/openai/anthropic/ollama); built via `dependencies._build_provider()`, cached with `lru_cache`, invalidated by `reset_providers()` on settings save
- `service/app/services/` — `grocy.py` (inventory client + `consume_by_barcode`), `mealie.py` (recipes/mealplan/shopping client + `suggest_recipes` inventory matcher), `barcode.py` (Open Food Facts + LLM enrichment), `defaults.py` (expiry rules), `current_recipe.py` (in-memory active recipe + Mealie-detail normalizer), `timers.py` (shared server-side timer registry), `recipe_timers.py` (parses step durations into timer suggestions), `recipes_import.py` (parse a recipe file: generic JSON / schema.org JSON-LD / Mealie export), `cameras.py` (resolve a configured camera to a fetchable feed; HA cameras need a bearer header, not a token in the URL, so the app proxies them), `scanner_mode.py` (in-memory barcode scanner mode: inventory / consume / shopping / audit), `audit.py` (in-memory, location-scoped read-only pantry stock count: scans compared to Grocy stock, never written back), `nutrition.py` (food-intake log totals; pairs with the `IntakeLog` model), `weather.py` (kiosk forecast: Open-Meteo primary, wttr.in fallback; pure parse helpers), `satellite.py` (pi_remote pulls + persists backend config from the main server), `devices.py` (main-server registry of satellite remotes + up-to-date/behind version badge via `version_compare.py`), `ha_events.py` (in-memory ring of on-screen HA events: notification toasts + camera pop-ups, polled by the kiosk), plus `utensils.py` / `action_items.py` / `camera_scan.py` / `lan_scan.py` (recipe-equipment match, action inbox, IP-camera probe, LAN instance sweep)
- `service/app/routers/` — REST + UI routes; `templates/` are Bootstrap 5 dark-theme Jinja2. `current_recipe.py` serves the Current Recipe (nav label "On the Line") + `/timers` API; `admin.py` has backup + `POST /admin/restore` (app data, zip-slip guarded, secret-preserve-on-blank); `ui.py` serves the kiosk pages incl. `/ui/camera` (+ the `/ui/camera/{i}/snapshot|stream` HA proxy and `/ui/camera/diag`), `/ui/weather` (+ `/ui/weather/data`), `/ui/convert`, `/ui/kitchen-guide`, `/ui/timers`, `/ui/audit`, `/ui/nutrition`, `/ui/journal`; `audit.py` (`/audit/*`, satellite-forwarding read-only stock count) and `nutrition.py` (`/nutrition/*` intake log) back those pages; `pending.py` has `/pending/scan` (routes by scanner mode) + `/pending/scanner-mode` (+ `/cycle`); `events.py` is the HA event channel (`POST /events/notify` + `/events/camera-popup`, `GET /events/poll`) shown on screen by `static/js/ha-events.js` when `ha_events_enabled`. Browser-facing static JS lives in `static/js/` (`floating-nav.js`, `timer-window.js`, `kiosk-idle.js`, `kiosk-auto.js`, `ha-events.js`)
- `homeassistant/` — REST sensor config, automations (barcode scanner via keyboard_remote), Lovelace dashboard
- `scripts/image-build/` — Pi provisioning: `firstboot.sh`, the `foodassistant-host-bridge` (host root helper at `127.0.0.1:9299`), and the OTA/restore helpers `foodassistant-update` (redeploys app + Stream Deck, re-runnable after a manual pull) and `foodassistant-restore` (full Grocy+Mealie snapshot restore from a path or `rclone:` source, driven by the bridge `POST /restore`)

## Conventions & Gotchas

- **HA sensors use the LAN URL** (`http://192.168.1.170:9284`), never the Pangolin public URL (headless requests get an HTML redirect). Lovelace buttons use the public URL.
- App code is volume-mounted with `--reload`: `git pull` applies changes live; rebuild only for `requirements.txt`/Dockerfile changes.
- Mealie client auto-detects v1 (`/api/groups/`) vs v2 (`/api/households/`) API paths.
- HA template gotcha: compare `key_code` as integers (`key == 28`), never cast with `| string`.
- LLM JSON replies may be fenced — always parse with `providers.base.parse_json_response`.
- Current Recipe and timers are **in-memory and process-local** (no disk persistence); a restart clears them. Keep the normalization/scaling/parse logic pure so it stays testable.
- Templates get an `ai_configured` flag from `templating.theme_context`; gate AI-only UI on it (`{% if ai_configured %}`) so the app never offers actions that cannot work without a provider.
- `/setup/save` applies only the fields present in the request (`model_dump(exclude_unset=True)`), so the per-section Save buttons post just their own fields without clobbering others.

## Build & Test

```bash
docker compose up -d --build                 # run (add profiles as needed)
# local smoke test deps:
pip install fastapi jinja2 itsdangerous pillow python-multipart sqlalchemy pydantic-settings httpx
python -c "import sys; sys.path.insert(0,'service'); from app.main import app"
```

Tests: `pip install pytest && python -m pytest tests/ -q` — staple matching,
tier classifier, LLM JSON parsing (pure logic, no network/Docker needed).
Backups: `./scripts/backup.sh [dest]` (cron-friendly, 14-day rotation);
restore with `./scripts/restore.sh <archive>`.
