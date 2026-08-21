# delta-ops-board

A small Docker service that tracks Delta Force's competitive esports scene from public data, polling on a schedule. Cloned from [r6-ops-board](https://github.com/Revanito/r6-ops-board) as a starting base, then rewritten for Delta Force's own scene and data sources.

**🎮 Live page: [delta.vaultinc.fr](https://delta.vaultinc.fr/)** (also reachable at [revanito.github.io/delta-ops-board](https://revanito.github.io/delta-ops-board/) — GitHub Pages serves both, one isn't a redirect of the other)

## What it does

**`delta-site`** — regenerates two static pages and pushes them to `docs/` on this repo's `main` branch, only when the rendered pages actually changed. GitHub Pages serves that folder.

Delta Force's esports scene runs two parallel tracks by game mode, with different formats and different data sources, so the site is split into two pages by mode rather than one combined page:

- **`index.html` (Warfare)** — the traditional team-vs-team mode. Live now, Upcoming (next 30 days), Results (a mix of recent ticker results and the full result set of whatever Warfare tournaments are in `BRACKET_PAGES`, see below).
- **`operations.html` (Operations)** — the extraction-shooter/lobby mode. Active drops, recent results across the whole Operations scene (winner-only, from Liquipedia), and a sidebar with RISE Series EMEA + Americas: standings table, top-5 MVPs, and the most recent lobby placements, each pulled from TiMi's own RISE Series backend.

Both pages share an "Active drops" section (real campaign name + exact date range, see Data sources below) and a small top nav to switch between them.

Rebuild frequency adapts automatically: every `SITE_BUILD_INTERVAL_MINUTES` (10) while a Warfare match is live or one's scheduled to start within `ACTIVE_LOOKAHEAD_HOURS` (48h), otherwise every `SITE_IDLE_BUILD_INTERVAL_MINUTES` (24h) - so quiet stretches between tournaments don't spam commits or Pages rebuilds for pages that aren't changing anyway. This "is an event going on" check only looks at the Warfare track (see Known gaps below for why).

## Data sources

- **[Liquipedia](https://liquipedia.net/deltaforce/Liquipedia:Matches)** — the site-wide match ticker, covering both tracks. Liquipedia renders them with two different templates: 2-team head-to-head matches (Warfare) and battle-royale-style lobby results (Operations) that only ever expose the winner in this feed, not full standings.
- **Liquipedia tournament pages** (`BRACKET_PAGES` in `sources.py`) — the ticker rarely carries Warfare entries (Operations events are far more frequent), so a Warfare tournament's own page is fetched directly for its bracket. **This list is maintained by hand** - there's no "here's the currently active event" feed for Delta Force the way Ubisoft's page gave r6-ops-board that for free. After a new Warfare LAN/qualifier, add its Liquipedia page title to `BRACKET_PAGES` or its results won't show up. Check [Portal:Tournaments](https://liquipedia.net/deltaforce/Portal:Tournaments) for the current S/A-tier Warfare entries.
- **[api-dfgw.timi-es.com](https://api-dfgw.timi-es.com)** — TiMi's own backend for RISE Series (undocumented but public, no auth needed; discovered behind playdeltaforce.com's RISE Series page). Standings, rosters, schedule/lobby results, and MVP ranking. Confirmed working for RISE Series EMEA + Americas only - don't assume other Operations tournaments run on this backend without checking.
- **[twitchdrops.app](https://twitchdrops.app/game/delta-force-hawk-ops)** — active drop rewards and eligible channels. Note the URL slug is the game's full Twitch category name ("Delta Force: Hawk Ops"), not `delta-force`.
- **playdeltaforce.com's drops calendar** (`activitymaps.json`, fetched straight off the game's own drops microsite) — the official campaign name + exact date range per drops event. twitchdrops.app's own campaign field comes back blank, so this is the only source with real dates; used to label the "Active drops" section.

Two sources exist for other games in this family but have no Delta Force equivalent: no Ubisoft-style official schedule page with a trustworthy `live` flag, and no siege.gg-style stats site with flags/logos/bracket API (`deltaforceesports.com` and `escharts.com` were both checked and ruled out - see Known gaps).

## Known gaps

Delta Force's esports infrastructure is thinner than Rainbow Six's, and these aren't just "not built yet":

- **No confirmed "live right now" signal.** r6-ops-board trusted Ubisoft's official feed for this. Delta Force has nothing equivalent, so a match is only tentatively marked live off Liquipedia's own ticker state (a past start time with no result posted yet, within a few hours).
- **No automatic bracket discovery.** See `BRACKET_PAGES` above - has to be updated by hand after each new Warfare event.
- **No team flags or logos** for the Warfare track. No data source has been found for this yet.
- **escharts.com and teamrrq.com were both ruled out** as automated sources - both sit behind Cloudflare's managed JS challenge, which a plain HTTP scraper can't get past (and isn't worth building around).

## Setup

1. Register a free Twitch app at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) to get a Client ID + Secret (Client Credentials flow - no user login, no special permissions, just app registration). Optional - the site works fine without it, it just skips live-status/drops-badge extras.
2. Generate a fine-grained GitHub Personal Access Token scoped to just this repo, with **Contents: Read and write** — Settings → Developer settings → Personal access tokens → Fine-grained tokens.
3. Enable GitHub Pages on this repo: Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, folder: `/docs` → Save. (The page won't render until `delta-site` pushes its first `docs/index.html` — see below.)
4. Copy `.env.example` to `.env` and fill in the values from steps 1–2.
5. `docker compose up -d --build`

State lives entirely in the site generator's own git clone at `./site-repo/` - there's no separate state file, since (unlike r6-ops-board) there's no Discord notifier here tracking what's already been announced.

**Optional: custom domain.** Add a `CNAME` record for your subdomain pointing at `<username>.github.io` (no trailing content, GitHub's own DNS resolves the rest), then Settings → Pages → Custom domain → enter the subdomain → Save. GitHub writes a `CNAME` file into `docs/` automatically once saved - `webgen.py` never touches or deletes files it didn't write, so it survives every future rebuild untouched. GitHub auto-issues an HTTPS cert once its DNS check passes (usually minutes after the DNS record itself has propagated); the plain `github.io` URL keeps serving the exact same content the whole time, unaffected.

## Notes

- `webgen.py` is the site generator - deliberately not named `site.py`, since that shadows Python's built-in `site` module.
- `sources.py` holds all the fetch/parse logic.
- Set `RUN_ON_START=true` in `.env` to make the service build once immediately on startup instead of waiting for its first scheduled slot - useful when testing.
- `TWITCH_CHANNELS` defaults to the official channel (`deltaforcegameofficial`) if left blank in `.env` - set it to override, not to opt in.
- Unlike r6-ops-board, there's no drops/events archive here yet (no `archive/*.json`, no `drops.html`/`events.html` pages) - a reasonable future addition, following the same pattern, if it turns out to be wanted.
