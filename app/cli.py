"""Command line helpers.

    python -m app.cli run                       # run the daily scan now
    python -m app.cli search "gameboy" --near "Dallas, TX" --radius-mi 20
    python -m app.cli fb-login                  # log in to Facebook in a visible browser, save cookies
    python -m app.cli fb-import-cookies cookies.json   # convert a browser-extension cookie export
    python -m app.cli seed-demo                 # example members + one scan, to try the app
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import settings
from .models import KM_PER_MILE


def cmd_run(_args) -> int:
    from .db import init_db
    from .jobs import run_scrape
    from .db import SessionLocal
    from .models import ScrapeRun

    init_db()
    run_id = run_scrape(trigger="cli")
    if run_id is None:
        print("A scan is already running.")
        return 1
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        print(run.log)
        return 0 if run.status in ("ok", "partial") else 1


def cmd_search(args) -> int:
    from .geocode import geocode
    from .sources import Area, get_source

    place = geocode(args.near)
    radius_km = args.radius_km or (args.radius_mi or 20) * KM_PER_MILE
    area = Area(lat=place.lat, lng=place.lng, radius_km=radius_km, label=place.label, fb_location=args.fb_location)
    print(f"Searching {settings.source!r} for {args.query!r} within {radius_km:.0f} km of {place.label} ({place.lat}, {place.lng})")
    from .sources import SourceError

    try:
        with get_source(settings) as src:
            results = src.search(args.query, area)
    except SourceError as exc:
        print(f"Failed: {exc}")
        return 1
    for r in results:
        print(f"- {r.price_text or '?':>10}  {r.title}  [{r.location_text or ''}]  {r.url}")
    print(f"{len(results)} results")
    return 0


def cmd_fb_login(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out or settings.fb_storage_state)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        page.goto("https://www.facebook.com/login")
        input("Log in to Facebook in the browser window (finish any 2FA), then press Enter here… ")
        context.storage_state(path=str(out))
        browser.close()
    out.chmod(0o600)
    print(f"Saved session to {out}. Copy it to the server's FB_STORAGE_STATE path.")
    return 0


_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}


def convert_cookie_export(raw: list[dict]) -> dict:
    """Cookie-Editor / EditThisCookie JSON export -> Playwright storage_state."""
    cookies = []
    for c in raw:
        if "facebook.com" not in c.get("domain", ""):
            continue
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": _SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
            "expires": float(c.get("expirationDate") or c.get("expires") or -1),
        }
        cookies.append(cookie)
    return {"cookies": cookies, "origins": []}


def cmd_fb_import(args) -> int:
    raw = json.loads(Path(args.file).read_text())
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    state = convert_cookie_export(raw)
    names = {c["name"] for c in state["cookies"]}
    if not {"c_user", "xs"} <= names:
        print("Warning: the export has no c_user/xs cookies; it probably isn't from a logged-in session.")
    out = Path(args.out or settings.fb_storage_state)
    out.write_text(json.dumps(state, indent=1))
    out.chmod(0o600)
    print(f"Wrote {len(state['cookies'])} cookies to {out}")
    return 0


DEMO_PEOPLE = [
    # name, email, area label, lat, lng, radius km, contact, searches
    ("Amélie Laurent", "amelie@example.com", "Paris, Ile-de-France, France", 48.8535, 2.3484, 25, "Slack @amelie",
     ["gameboy mega man", "polaroid sx-70"]),
    ("Bob Turner", "bob@example.com", "Dallas, Texas, United States", 32.7763, -96.7969, 32, "Slack @bobt",
     ["le creuset dutch oven"]),
    ("Cara Nguyen", "cara@example.com", "Portland, Oregon, United States", 45.5202, -122.6742, 32, "text me",
     ["herman miller aeron"]),
]


def cmd_seed_demo(_args) -> int:
    """Create the three example members (password: password123) and run one scan."""
    from .db import SessionLocal, init_db
    from .jobs import run_scrape
    from .models import Search, User
    from .security import hash_password

    init_db()
    with SessionLocal() as db:
        first = db.query(User).count() == 0
        for i, (name, email, label, lat, lng, km, contact, queries) in enumerate(DEMO_PEOPLE):
            if db.query(User).filter_by(email=email).first():
                continue
            user = User(name=name, email=email, password_hash=hash_password("password123"),
                        is_admin=first and i == 0, is_helper=True, area_query=label.split(",")[0],
                        area_label=label, lat=lat, lng=lng, radius_km=km,
                        radius_unit="km" if "France" in label else "mi", contact=contact)
            db.add(user)
            db.flush()
            for q in queries:
                db.add(Search(user_id=user.id, query=q, max_price=150))
        db.commit()
    run_scrape(trigger="cli", sleep=lambda s: None)
    print("Demo members created (password: password123): " + ", ".join(p[1] for p in DEMO_PEOPLE))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daily scan now").set_defaults(fn=cmd_run)

    p = sub.add_parser("search", help="try one search with the configured source")
    p.add_argument("query")
    p.add_argument("--near", required=True, help="city or postal code")
    p.add_argument("--radius-mi", type=float)
    p.add_argument("--radius-km", type=float)
    p.add_argument("--fb-location", help="Marketplace slug or id (facebook source)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("fb-login", help="log in to Facebook in a visible browser and save the session")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_fb_login)

    p = sub.add_parser("fb-import-cookies", help="import a Cookie-Editor JSON export")
    p.add_argument("file")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_fb_import)

    sub.add_parser("seed-demo", help="create example members and run a scan (for trying the app)").set_defaults(
        fn=cmd_seed_demo
    )

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
