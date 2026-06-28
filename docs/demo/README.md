# FoodAssistant interactive demo

`index.html` is a self-contained, no-backend interactive demo of FoodAssistant
(inventory, scanning, recipe suggestions, cameras, the unit converter, and the
Stream Deck). It runs entirely in the browser with sample data; open it locally
or host it as a static site.

## Live demo (Cloudflare)

The demo is deployed from `docs/demo` as a Cloudflare **Workers Static Assets**
site, configured by [`wrangler.toml`](../../wrangler.toml) at the repo root. With
the Cloudflare dashboard connected to this repo, every push to `main` rebuilds
and redeploys it automatically, so the published demo stays in step with the
repo.

After the first deploy it lives at
`https://foodassistant-demo.<your-subdomain>.workers.dev` (add a custom domain in
the dashboard if you want a nicer URL).

### Dashboard "Connect to Git" settings (Workers Builds)

| Field | Value |
| --- | --- |
| Project name | `foodassistant-demo` |
| Build command | *(leave blank)* |
| Deploy command | `npx wrangler deploy` |
| Non-production branch deploy command | `npx wrangler versions upload` (or blank) |
| Path | `/` |
| Variables / secrets | none needed |

`wrangler.toml` points `[assets].directory` at `./docs/demo` and defines a
static, script-less Worker, so `npx wrangler deploy` publishes the folder with no
build step.

The build token's missing-permission warning (ssl_and_certificates, ai_search,
connectivity_directory) is unrelated to a static deploy and can be ignored; those
are for features this demo does not use.

## Updating the demo

The demo is a hand-built static mock, not the live app, so it does not track
features automatically. When a feature is worth showcasing, edit `index.html`;
the push then redeploys it.
