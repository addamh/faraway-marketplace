import re

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import SessionLocal
from app.geocode import Place
from app.jobs import run_scrape
from app.models import Match, User

PLACES = {
    "Paris": Place(48.8566, 2.3522, "Paris, Ile-de-France, France"),
    "Dallas, TX": Place(32.7767, -96.797, "Dallas, Texas, United States"),
    "Portland, OR": Place(45.5152, -122.6784, "Portland, Oregon, United States"),
}


@pytest.fixture(autouse=True)
def fake_geocode(monkeypatch):
    monkeypatch.setattr(main, "geocode", lambda q: PLACES[q])


def csrf(client, path):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup(name, email, area, invite="letmein"):
    c = TestClient(main.app)
    r = c.post("/register", data={"csrf": csrf(c, "/register"), "name": name, "email": email,
                                  "password": "password123", "invite": invite})
    assert r.status_code == 200, r.text
    if area:
        c.post("/profile", data={"csrf": csrf(c, "/profile"), "name": name, "is_helper": "on", "area": area,
                                 "radius": "20", "radius_unit": "mi"})
    return c


def test_end_to_end_example():
    with TestClient(main.app) as boot:  # runs lifespan (creates tables)
        boot.get("/healthz")
    amelie = signup("Amélie", "a@example.com", "Paris")
    bob = signup("Bob", "b@example.com", "Dallas, TX")
    cara = signup("Cara", "c@example.com", "Portland, OR")

    with SessionLocal() as db:
        users = {u.name: u for u in db.query(User).all()}
        assert users["Amélie"].is_admin and not users["Bob"].is_admin
        assert users["Bob"].area_label == "Dallas, Texas, United States"
        assert round(users["Bob"].radius_km) == 32 and users["Bob"].radius_display == "20 mi"

    r = amelie.post("/searches", data={"csrf": csrf(amelie, "/searches"), "query": "gameboy mega man",
                                      "max_price": "100", "strict": "on"})
    assert "gameboy mega man" in r.text and "Bob" in r.text and "Cara" in r.text

    run_scrape(trigger="test", sleep=lambda s: None)

    page = amelie.get("/finds").text
    assert "Near Bob" in page and "Near Cara" in page and "Near Amélie" not in page
    assert "Gameboy Mega Man" in page
    assert "Phone case" not in page  # demo junk filtered by strict matching
    assert "mailto:b@example.com" in page

    with SessionLocal() as db:
        matches = db.query(Match).all()
        assert matches and {m.helper.name for m in matches} == {"Bob", "Cara"}
        listing_id = matches[0].listing_id

    # Helpers see what was found in their area for others.
    assert "For Amélie" in bob.get("/my-area").text

    # Save, hide, mark seen.
    token = csrf(amelie, "/finds")
    amelie.post(f"/finds/{listing_id}/status", data={"csrf": token, "status": "hidden", "next": "/finds"})
    assert listing_id not in amelie.get("/finds?show=all").text
    assert listing_id in amelie.get("/finds?show=hidden").text
    amelie.post("/finds/mark-seen", data={"csrf": token})
    with SessionLocal() as db:
        assert not db.query(Match).filter(Match.status == "new").count()

    # Bob can't touch Amélie's searches or the admin page.
    assert bob.get("/admin").status_code == 403
    assert amelie.get("/admin").status_code == 200


def test_invite_code_and_csrf_are_enforced():
    signup("Admin", "admin@example.com", None)
    c = TestClient(main.app)
    token = csrf(c, "/register")
    r = c.post("/register", data={"csrf": token, "name": "Eve", "email": "e@example.com",
                                  "password": "password123", "invite": "wrong"})
    assert "invite code isn" in r.text
    r = c.post("/register", data={"csrf": "forged", "name": "Eve", "email": "e@example.com",
                                  "password": "password123", "invite": "letmein"})
    assert r.status_code == 403


def test_login_required_and_open_redirect_blocked():
    c = TestClient(main.app)
    assert c.get("/finds", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/login", follow_redirects=False).headers["location"] == "/register"  # no users yet -> setup
    a = signup("Admin", "admin@example.com", None)
    r = a.post("/finds/mark-seen", data={"csrf": csrf(a, "/finds"), "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/finds"


def test_demo_image_is_svg():
    r = TestClient(main.app).get("/demo-image/123.svg")
    assert r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text
