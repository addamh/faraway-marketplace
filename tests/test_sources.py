import json
from pathlib import Path

import httpx
import pytest

from app.cli import convert_cookie_export
from app.sources.base import Area, parse_price, SourceAuthError
from app.sources.facebook import (
    build_search_url,
    listings_from_html,
    listings_from_json_texts,
    listings_from_links,
    normalize_fb_location,
)
from app.sources.serpapi import SerpApiSource

FIX = Path(__file__).parent / "fixtures"
DALLAS = Area(lat=32.7767, lng=-96.797, radius_km=32, label="Dallas, Texas, United States")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$25", 25.0),
        ("$1,200", 1200.0),
        ("25 €", 25.0),
        ("1.234,50 €", 1234.5),
        ("1 200 €", 1200.0),
        ("€1.200", 1200.0),
        ("CA$12.99", 12.99),
        ("Free", 0.0),
        ("", None),
        ("Contact seller", None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("dallas", "dallas"),
        ("https://www.facebook.com/marketplace/dallas/search/?query=x", "dallas"),
        ("www.facebook.com/marketplace/112763262074197/", "112763262074197"),
        ("/marketplace/paris", "paris"),
        ("https://www.facebook.com/marketplace/item/123/", None),
        ("https://www.facebook.com/marketplace/", None),
        ("not a slug!", None),
        ("", None),
    ],
)
def test_normalize_fb_location(value, expected):
    assert normalize_fb_location(value) == expected


def test_build_search_url_prefers_explicit_location():
    assert build_search_url("mega man", DALLAS) == (
        "https://www.facebook.com/marketplace/dallas/search/?query=mega%20man&radius=32"
    )
    area = Area(lat=0, lng=0, radius_km=16.09, label="Portland, Oregon", fb_location="105462436157046")
    assert "/marketplace/105462436157046/search/" in build_search_url("x", area)
    assert "radius=16" in build_search_url("x", area)


def test_listings_from_embedded_html():
    found = {r.id: r for r in listings_from_html((FIX / "fb_search.html").read_text())}
    assert set(found) == {"1111111111111111", "2222222222222222"}
    mm = found["1111111111111111"]
    assert mm.title == "Mega Man Gameboy cartridge"
    assert mm.price_text == "$25" and mm.price_amount == 25.0
    assert mm.location_text == "Dallas, TX"
    assert mm.image_url == "https://scontent.example/1.jpg"
    assert mm.url == "https://www.facebook.com/marketplace/item/1111111111111111/"
    assert found["2222222222222222"].is_sold


def test_listings_from_graphql_bodies():
    found = {r.id: r for r in listings_from_json_texts([(FIX / "fb_graphql.txt").read_text()])}
    assert found["3333333333333333"].price_amount == 40.0
    assert found["3333333333333333"].location_text == "Paris"
    assert found["4444444444444444"].is_pending
    assert found["4444444444444444"].price_amount == 0.0


def test_listings_from_links_fallback():
    links = [
        {"href": "/marketplace/item/555/?ref=search", "text": "$30\n$45\nMega Man X Gameboy\nDallas, TX", "img": "https://x/5.jpg"},
        {"href": "/marketplace/item/555/", "text": "dup", "img": None},
        {"href": "/marketplace/item/666/", "text": "Free\nBox of games\nPlano, TX", "img": None},
        {"href": "/groups/123", "text": "not a listing"},
    ]
    out = {r.id: r for r in listings_from_links(links)}
    assert set(out) == {"555", "666"}
    assert out["555"].title == "Mega Man X Gameboy" and out["555"].price_amount == 30.0
    assert out["555"].location_text == "Dallas, TX"
    assert out["666"].price_amount == 0.0


def _serp(handler):
    return SerpApiSource("k", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_serpapi_parses_organic_results_and_sends_location():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={
            "organic_results": [
                {"listing_id": "952918357338100", "title": "Mega Man GB", "link": "https://www.facebook.com/marketplace/item/952918357338100/",
                 "price": "$110", "extracted_price": 110, "location": "Dallas, TX", "thumbnail": "https://img/1.jpg", "is_pending": True},
                {"title": "no id, skipped"},
            ],
            "relaxed_results": [{"listing_id": "1", "title": "far away"}],
        })

    out = _serp(handler).search("mega man", DALLAS)
    assert [r.id for r in out] == ["952918357338100"]
    assert out[0].price_amount == 110.0 and out[0].is_pending and out[0].image_url == "https://img/1.jpg"
    assert seen["engine"] == "facebook_marketplace" and seen["q"] == "mega man"
    assert seen["lat"] == "32.77670" and seen["lon"] == "-96.79700" and seen["radius"] == "32"


def test_serpapi_no_results_is_empty_and_bad_key_is_fatal():
    assert _serp(lambda r: httpx.Response(200, json={"error": "Facebook Marketplace hasn't returned any results for this query."})).search("x", DALLAS) == []
    with pytest.raises(SourceAuthError):
        _serp(lambda r: httpx.Response(401, json={"error": "Invalid API key."})).search("x", DALLAS)


def test_cookie_export_conversion():
    state = convert_cookie_export([
        {"domain": ".facebook.com", "name": "c_user", "value": "1", "path": "/", "secure": True, "httpOnly": False,
         "sameSite": "no_restriction", "expirationDate": 1999999999.5},
        {"domain": ".example.com", "name": "other", "value": "x"},
    ])
    assert [c["name"] for c in state["cookies"]] == ["c_user"]
    assert state["cookies"][0]["sameSite"] == "None"
    json.dumps(state)
