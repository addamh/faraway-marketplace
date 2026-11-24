from app.config import Settings
from app.db import SessionLocal
from app.jobs import plan_tasks, run_scrape
from app.models import Listing, Match, ScrapeRun, Search, User
from app.sources.base import RawListing, Source, SourceAuthError, SourceError


def L(i, title, price=20.0, sold=False):
    return RawListing(id=str(i), title=title, url=f"https://www.facebook.com/marketplace/item/{i}/",
                      price_text=f"${price:g}", price_amount=price, location_text="x", is_sold=sold)


class FakeSource(Source):
    name = "fake"
    delay_range = (1, 2)

    def __init__(self, by_city):
        self.by_city = by_city
        self.calls = []

    def search(self, query, area):
        self.calls.append((query, area.label))
        result = self.by_city.get(area.label)
        if isinstance(result, Exception):
            raise result
        return list(result or [])


def setup_people():
    """The README example: A in Paris is hunting; B (Dallas) and C (Portland) can pick up."""
    with SessionLocal() as db:
        a = User(email="a@x", name="Amélie", password_hash="x", is_helper=True, area_label="Paris",
                 lat=48.85, lng=2.35, radius_km=30)
        b = User(email="b@x", name="Bob", password_hash="x", is_helper=True, area_label="Dallas",
                 lat=32.78, lng=-96.8, radius_km=32)
        c = User(email="c@x", name="Cara", password_hash="x", is_helper=True, area_label="Portland",
                 lat=45.52, lng=-122.68, radius_km=32)
        d = User(email="d@x", name="Dee (no area)", password_hash="x", is_helper=False)
        db.add_all([a, b, c, d])
        db.flush()
        s = Search(user_id=a.id, query="gameboy mega man", max_price=100, exclude="repro")
        db.add(s)
        db.commit()
        return a.id, b.id, c.id, s.id


def cfg(**kw):
    c = Settings()
    c.include_own_area = False
    c.max_requests_per_run = 100
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_plan_skips_own_area_and_non_helpers():
    a, b, c, _ = setup_people()
    with SessionLocal() as db:
        tasks = plan_tasks(db, cfg())
    assert sorted(t.helper_id for t in tasks) == sorted([b, c])
    with SessionLocal() as db:
        assert len(plan_tasks(db, cfg(include_own_area=True))) == 3


def test_run_matches_filters_and_dedupes():
    a, b, c, sid = setup_people()
    src = FakeSource({
        "Dallas": [
            L(1, "Mega Man Gameboy cartridge", 25),
            L(2, "Phone case"),  # not all words
            L(3, "MegaMan GAME BOY (repro)"),  # excluded word
            L(4, "Gameboy Mega Man complete", 250),  # over max price
            L(5, "Game Boy Megaman sold", sold=True),
        ],
        "Portland": [L(6, "Lot: gameboy + mega man 2", 60), L(1, "Mega Man Gameboy cartridge", 25)],
    })
    sleeps = []
    run_id = run_scrape(source=src, cfg=cfg(), sleep=sleeps.append)
    assert len(src.calls) == 2 and len(sleeps) == 1  # polite delay between requests
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        assert run.status == "ok" and run.requests_made == 2 and run.new_matches == 3
        pairs = {(m.listing_id, m.helper_id) for m in db.query(Match).all()}
        # Listing 1 showed up in both areas, so either helper could grab it.
        assert pairs == {("1", b), ("6", c), ("1", c)}
        assert db.query(Listing).count() == 2
        assert db.get(Search, sid).last_run_at is not None

    # Second run: nothing new, nothing duplicated.
    run_id = run_scrape(source=src, cfg=cfg(), sleep=lambda s: None)
    with SessionLocal() as db:
        assert db.get(ScrapeRun, run_id).new_matches == 0
        assert db.query(Match).count() == 3


def test_run_continues_past_errors_and_stops_on_auth_failure():
    setup_people()
    src = FakeSource({"Dallas": SourceError("timeout"), "Portland": [L(6, "gameboy mega man", 10)]})
    run_id = run_scrape(source=src, cfg=cfg(), sleep=lambda s: None)
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        assert run.status == "partial" and run.new_matches == 1 and "timeout" in run.log

    src = FakeSource({"Dallas": SourceAuthError("cookies expired"), "Portland": SourceAuthError("cookies expired")})
    run_id = run_scrape(source=src, cfg=cfg(), sleep=lambda s: None)
    with SessionLocal() as db:
        run = db.get(ScrapeRun, run_id)
        assert run.status == "failed" and len(src.calls) == 1 and "cookies expired" in run.log


def test_request_cap_postpones_the_rest_and_rotates():
    setup_people()
    src = FakeSource({})
    run_id = run_scrape(source=src, cfg=cfg(max_requests_per_run=1), sleep=lambda s: None)
    with SessionLocal() as db:
        assert "postponed" in db.get(ScrapeRun, run_id).log
    run_scrape(source=src, cfg=cfg(max_requests_per_run=1), sleep=lambda s: None)
    run_scrape(source=src, cfg=cfg(max_requests_per_run=1), sleep=lambda s: None)
    # One request per run, alternating between the two areas.
    assert [label for _, label in src.calls] in (["Dallas", "Portland", "Dallas"], ["Portland", "Dallas", "Portland"])
