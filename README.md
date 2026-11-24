# Faraway Finds

Search Facebook Marketplace in your friends' and coworkers' towns.

Members who are happy to pick things up register their city and how far they'll travel.
Members who are hunting for something register searches. Once a day the app runs every search
in every *other* member's pickup area, keeps the relevant hits, and shows each buyer what turned
up, grouped by the person who could grab it. Buying, pickup and shipping happen offline.

> Amélie (Paris) searches "gameboy mega man". Bob lives in Dallas (20 mi), Cara in Portland
> (20 mi). Each morning the app searches Marketplace around Dallas and Portland. Amélie's finds
> page shows *Near Bob · Dallas* and *Near Cara · Portland*, each with photo, title, price, a link
> to the listing and an "Ask Bob" button that drafts the email.

## Quick start (demo data)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m app.cli seed-demo      # optional: Amélie/Bob/Cara example, password "password123"
uvicorn app.main:app --reload
```

Open http://localhost:8000. With no members yet, the first account you register becomes the
admin; the Admin page has the invite link for everyone else.

The default `SOURCE=demo` produces fake listings so you can try everything without touching
Facebook. Pick a real source before inviting people.

## Choosing a data source

Facebook has no public Marketplace API, and from server/datacenter IPs it sends logged-out
visitors straight to the login page (verified while building this). So real data needs one of:

| `SOURCE=` | How it works | Cost | Risk |
|---|---|---|---|
| `serpapi` | [SerpApi's Marketplace API](https://serpapi.com/facebook-marketplace-api): takes lat/lon + radius, returns JSON | Paid per search; see their plans | Low. Their problem to keep working |
| `facebook` | Headless Chromium using the cookies of a logged-in Facebook account | Free | Facebook's terms forbid automated collection; the account may get checkpointed or disabled. Breaks when Facebook changes its pages |
| `demo` | Fake listings | Free | None |

**Requests per scan = active searches × pickup areas** (the buyer's own area is skipped).
5 people with 3 searches each and 5 pickup areas is about 60 requests a day, ~1,800 a month.
`MAX_REQUESTS_PER_RUN` caps a scan; anything over the cap rolls to the next day, least recently
checked first.

### SerpApi

```env
SOURCE=serpapi
SERPAPI_KEY=...
```

Searches use each helper's geocoded city center and radius directly.

### Facebook cookies

The scraper needs a saved login session. Two ways to get one:

1. **On a computer with a screen** (with this repo and requirements installed):
   `python -m app.cli fb-login` opens a browser; log in, press Enter, and copy the resulting
   `data/fb_state.json` to the server's `FB_STORAGE_STATE` path.
2. **From your normal browser**: export facebook.com cookies as JSON with an extension such as
   Cookie-Editor, then `python -m app.cli fb-import-cookies cookies.json`.

Then set `SOURCE=facebook`. Searches run one at a time, 20–45 seconds apart, so a 60-request
scan takes about half an hour. When the cookies expire, the scan stops and the Admin page shows
"redirected to its login/checkpoint page".

Location on Facebook works by city slug in the URL (`/marketplace/dallas/search/...`). The app
derives it from the helper's city name, which works for many large cities. If a helper's area
gets no results, have them open Marketplace, set their location, search for anything, and paste
that URL into the "Marketplace location" field on their profile (only shown with this source).

Not tested against a live logged-in session: this was built in an environment without a
Facebook account. The parser is deliberately shape-agnostic (any JSON object carrying
`marketplace_listing_title` + `id`, with a rendered-link fallback), but expect to adjust it.
Try one search first:

```bash
SOURCE=facebook python -m app.cli search "gameboy mega man" --near "Dallas, TX" --radius-mi 20
```

## Deploying

One small always-on server is enough (the scheduler runs inside the web process, so run a
single instance/worker).

```bash
cp .env.example .env     # fill it in
docker compose up -d --build
```

Data (SQLite database, cached thumbnails, cookies, generated secret key) lives in `./data`.
Put it behind HTTPS (Caddy, nginx, or your host's proxy) and set `SECURE_COOKIES=true`.

If your host injects a `DATABASE_URL` for Postgres, either unset it or add a driver
(`pip install psycopg[binary]`); the app is tested on SQLite.

To run scans from cron instead of the built-in scheduler: `SCHEDULER_ENABLED=false` and
`python -m app.cli run` daily.

## How it works

- **Matching.** Marketplace search is loose, so results are filtered on our side: by default
  every word of the query must appear in the title (spacing-insensitive, so "mega man" matches
  "MegaMan"), plus optional must-include/exclude words and a price range. Sold listings are dropped.
- **Dedupe.** Listings are keyed by Marketplace item id. A listing found near two helpers shows
  under both, so the buyer can ask either.
- **New / seen / saved / hidden.** Each find starts as New; buyers can save, hide or mark all seen.
- **Photos.** Facebook image URLs expire within days, so thumbnails are cached in `data/images`.
- **Privacy.** Members see each other's names, emails, contact note, city and radius. Exact
  coordinates are never shown; geocoding uses OpenStreetMap Nominatim.
- **Prices** are shown as listed, in the local currency of each area.

```
app/
  main.py          routes (FastAPI, server-rendered Jinja templates)
  jobs.py          daily scan: plan (search × area), fetch, filter, store
  filters.py       relevance filtering
  sources/         demo.py, serpapi.py, facebook.py (pluggable, see base.py)
  geocode.py       city -> lat/lng via Nominatim
  cli.py           run / search / fb-login / fb-import-cookies / seed-demo
```

## Tests

```bash
pytest
```

Covers price parsing, the Facebook and SerpApi parsers (with Facebook-shaped fixtures), the
scan pipeline (the Paris/Dallas/Portland example, dedupe, error handling, request cap rotation)
and the web flows (invite codes, CSRF, permissions, finds actions).

## Ideas for later

- Email or Slack digest when new finds appear.
- Separate groups (e.g. "work" vs "friends") so people only search areas within a group.
- Per-search choice of which areas to search.
- Alembic migrations once the schema settles.
