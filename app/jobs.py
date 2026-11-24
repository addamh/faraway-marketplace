"""The daily job: run every active search in every helper's area and record what turned up."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import filters
from .config import Settings, settings as default_settings
from .db import SessionLocal
from .images import cache_image
from .models import AreaCheck, Listing, Match, ScrapeRun, Search, User, utcnow
from .sources import Area, RawListing, Source, SourceAuthError, SourceError, get_source

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


@dataclass
class Task:
    """One Marketplace request: a query in one helper's area, shared by every search that wants it."""

    query: str
    key: str  # normalized query
    helper_id: int
    area: Area
    search_ids: list[int] = field(default_factory=list)
    last_checked: datetime = datetime.min


def is_running() -> bool:
    return _run_lock.locked()


def plan_tasks(db: Session, cfg: Settings) -> list[Task]:
    searches = db.scalars(
        select(Search).where(Search.active.is_(True)).options(selectinload(Search.user))
    ).all()
    helpers = [h for h in db.scalars(select(User).where(User.is_helper.is_(True))).all() if h.has_area]
    tasks: dict[tuple[str, int], Task] = {}
    for search in searches:
        query = filters.normalize_query(search.query)
        if not query:
            continue
        for helper in helpers:
            if helper.id == search.user_id and not cfg.include_own_area:
                continue
            key = (query, helper.id)
            task = tasks.get(key)
            if task is None:
                area = Area(
                    lat=helper.lat,
                    lng=helper.lng,
                    radius_km=helper.radius_km,
                    label=helper.area_label or "",
                    fb_location=helper.fb_location,
                )
                task = tasks[key] = Task(query=search.query.strip(), key=query, helper_id=helper.id, area=area)
            task.search_ids.append(search.id)
    checks = {(c.query, c.helper_id): c.last_checked_at for c in db.scalars(select(AreaCheck))}
    for task in tasks.values():
        task.last_checked = checks.get((task.key, task.helper_id), datetime.min)
    # Least recently checked first, so a request cap rotates fairly across days.
    return sorted(tasks.values(), key=lambda t: (t.last_checked, t.key, t.helper_id))


def _upsert_listing(db: Session, raw: RawListing, now: datetime) -> Listing:
    listing = db.get(Listing, raw.id)
    if listing is None:
        listing = Listing(id=raw.id, url=raw.url, title=raw.title, first_seen_at=now)
        db.add(listing)
    listing.title = raw.title[:500]
    listing.url = raw.url
    listing.price_text = raw.price_text
    listing.price_amount = raw.price_amount
    listing.location_text = raw.location_text or listing.location_text
    if raw.image_url and raw.image_url != listing.image_url:
        listing.image_url = raw.image_url
        listing.image_file = None  # re-cache
    listing.is_sold = raw.is_sold
    listing.is_pending = raw.is_pending
    listing.last_seen_at = now
    return listing


def _record_results(db: Session, task: Task, results: list[RawListing], now: datetime) -> tuple[int, int]:
    """Store results and matches for one task. Returns (listings kept, new matches)."""
    searches = [db.get(Search, sid) for sid in task.search_ids]
    kept = new = 0
    for raw in results:
        hits = [s for s in searches if s is not None and filters.matches(s, raw)]
        if not hits:
            continue
        kept += 1
        _upsert_listing(db, raw, now)
        db.flush()
        for search in hits:
            match = db.scalar(
                select(Match).where(
                    Match.search_id == search.id,
                    Match.listing_id == raw.id,
                    Match.helper_id == task.helper_id,
                )
            )
            if match is None:
                db.add(Match(search_id=search.id, listing_id=raw.id, helper_id=task.helper_id,
                             first_seen_at=now, last_seen_at=now))
                new += 1
            else:
                match.last_seen_at = now
    for search in searches:
        if search is not None:
            search.last_run_at = now
    check = db.scalar(select(AreaCheck).where(AreaCheck.query == task.key, AreaCheck.helper_id == task.helper_id))
    if check is None:
        db.add(AreaCheck(query=task.key, helper_id=task.helper_id, last_checked_at=now))
    else:
        check.last_checked_at = now
    return kept, new


def _cache_thumbnails(db: Session, cfg: Settings, listing_ids: set[str], lines: list[str]) -> None:
    pending = [
        l for l in (db.get(Listing, i) for i in listing_ids)
        if l is not None and l.image_url and l.image_url.startswith("http") and not l.image_file
    ]
    if not pending:
        return
    ok = 0
    with httpx.Client(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for listing in pending:
            name = cache_image(client, listing.id, listing.image_url, cfg.images_dir)
            if name:
                listing.image_file = name
                ok += 1
    db.commit()
    lines.append(f"Cached {ok}/{len(pending)} thumbnails.")


def run_scrape(
    trigger: str = "manual",
    source: Source | None = None,
    cfg: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Run every search once. Returns the ScrapeRun id, or None if a run is already going."""
    cfg = cfg or default_settings
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        return _run(trigger, source, cfg, sleep)
    finally:
        _run_lock.release()


def _run(trigger: str, source: Source | None, cfg: Settings, sleep) -> int:
    lines: list[str] = []

    def note(msg: str) -> None:
        log.info(msg)
        lines.append(f"{utcnow():%H:%M:%S} {msg}")

    with SessionLocal() as db:
        run = ScrapeRun(trigger=trigger, source=source.name if source else cfg.source)
        db.add(run)
        db.commit()

        tasks = plan_tasks(db, cfg)
        skipped = max(0, len(tasks) - cfg.max_requests_per_run)
        tasks = tasks[: cfg.max_requests_per_run]
        note(f"{len(tasks)} searches to run" + (f" ({skipped} postponed by MAX_REQUESTS_PER_RUN)" if skipped else ""))

        failures = 0
        seen_ids: set[str] = set()
        fatal = False
        try:
            src = source or get_source(cfg)
        except SourceAuthError as exc:
            note(f"Can't start: {exc}")
            src, fatal = None, True

        if src is not None:
            with src:
                for i, task in enumerate(tasks):
                    if i and src.delay_range[1] > 0:
                        sleep(random.uniform(*src.delay_range))
                    area = task.area.label or f"{task.area.lat},{task.area.lng}"
                    try:
                        results = src.search(task.query, task.area)
                    except SourceAuthError as exc:
                        note(f"Stopping: {exc}")
                        fatal = True
                        break
                    except SourceError as exc:
                        failures += 1
                        note(f"“{task.query}” near {area}: {exc}")
                        continue
                    except Exception as exc:  # keep going; one bad page shouldn't sink the run
                        failures += 1
                        log.exception("unexpected error")
                        note(f"“{task.query}” near {area}: unexpected error {exc!r}")
                        continue
                    run.requests_made += 1
                    now = utcnow()
                    kept, new = _record_results(db, task, results, now)
                    seen_ids.update(r.id for r in results)
                    run.listings_seen += kept
                    run.new_matches += new
                    note(f"“{task.query}” near {area}: {len(results)} results, {kept} relevant, {new} new")
                    run.log = "\n".join(lines)
                    db.commit()

        if seen_ids:
            try:
                _cache_thumbnails(db, cfg, seen_ids, lines)
            except Exception:  # pragma: no cover
                log.exception("thumbnail caching failed")

        if fatal and run.requests_made == 0:
            run.status = "failed"
        elif fatal or failures:
            run.status = "partial"
        else:
            run.status = "ok"
        run.finished_at = utcnow()
        note(f"Done: {run.requests_made} requests, {run.new_matches} new finds.")
        run.log = "\n".join(lines)
        db.commit()
        return run.id


def start_background_run(trigger: str = "manual") -> bool:
    if is_running():
        return False
    threading.Thread(target=run_scrape, kwargs={"trigger": trigger}, daemon=True, name="scrape").start()
    return True
