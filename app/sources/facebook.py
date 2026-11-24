"""Scrape Facebook Marketplace search pages with a headless browser.

Facebook sends logged-out visitors from server/datacenter IPs straight to the login page, so
in practice this needs the cookies of a logged-in Facebook account (see README: "Facebook
cookies"). Listings are read from three places, most structured first:

1. JSON the page embeds in <script type="application/json"> tags (first page of results);
2. GraphQL responses fetched while we scroll (further pages);
3. the rendered result links, as a fallback if Facebook changes its JSON shape.

Facebook changes its front end often. Parsing is shape-agnostic on purpose: any JSON object
with a `marketplace_listing_title` and an `id` is treated as a listing.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .base import Area, RawListing, Source, SourceAuthError, SourceError, item_url, parse_price

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
_RESERVED = {"item", "search", "category", "you", "notifications", "inbox", "create", "saved", "buying", "selling"}
_ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
_SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


# --------------------------------------------------------------------------- locations


def normalize_fb_location(value: str | None) -> str | None:
    """Accept 'dallas', '112530898761236', or a pasted Marketplace URL; return the slug/id."""
    if not value:
        return None
    value = value.strip()
    if "facebook.com" in value or value.startswith("/"):
        if "facebook.com" in value:
            path = urlparse(value if "://" in value else "https://" + value).path
        else:
            path = value
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "marketplace" and parts[1].lower() not in _RESERVED:
            return parts[1]
        return None
    value = value.strip("/")
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) and value.lower() not in _RESERVED else None


def city_slug(label: str | None) -> str | None:
    """'Dallas, Texas, United States' -> 'dallas'. Works for many big cities; not all."""
    if not label:
        return None
    city = label.split(",")[0]
    slug = re.sub(r"[^a-z0-9]", "", city.lower())
    return slug or None


def build_search_url(query: str, area: Area) -> str:
    location = normalize_fb_location(area.fb_location) or city_slug(area.label)
    if not location:
        raise SourceError(
            "No Facebook Marketplace location for this area. Ask the helper to paste a "
            "Marketplace URL for their area on their profile."
        )
    radius = max(1, round(area.radius_km))  # Marketplace URLs take the radius in km
    return f"https://www.facebook.com/marketplace/{location}/search/?query={quote(query)}&radius={radius}"


# --------------------------------------------------------------------------- parsing


def iter_json_docs(text: str) -> Iterator[Any]:
    """GraphQL bodies can be prefixed with 'for (;;);' and/or be newline-delimited JSON."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        yield json.loads(text)
        return
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _walk(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "marketplace_listing_title" in cur and cur.get("id"):
                yield cur
                continue
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def listing_from_node(node: dict) -> RawListing | None:
    listing_id = str(node.get("id") or "")
    title = (node.get("marketplace_listing_title") or node.get("custom_title") or "").strip()
    if not listing_id.isdigit() or not title or node.get("is_hidden"):
        return None
    price = node.get("listing_price") or {}
    price_text = price.get("formatted_amount") if isinstance(price, dict) else None
    amount = None
    if isinstance(price, dict) and price.get("amount") not in (None, ""):
        try:
            amount = float(price["amount"])
        except (TypeError, ValueError):
            amount = None
    if amount is None:
        amount = parse_price(price_text)
    geo = _get(node, "location", "reverse_geocode") or {}
    location = ", ".join(p for p in (geo.get("city"), geo.get("state")) if p) or None
    image = _get(node, "primary_listing_photo", "image", "uri") or _get(
        node, "primary_listing_photo", "listing_image", "uri"
    )
    return RawListing(
        id=listing_id,
        title=title,
        url=item_url(listing_id),
        price_text=price_text,
        price_amount=amount,
        location_text=location,
        image_url=image,
        is_sold=bool(node.get("is_sold")),
        is_pending=bool(node.get("is_pending")),
    )


def listings_from_json_texts(texts: Iterable[str]) -> list[RawListing]:
    found: dict[str, RawListing] = {}
    for text in texts:
        if "marketplace_listing_title" not in text:
            continue
        for doc in iter_json_docs(text):
            for node in _walk(doc):
                listing = listing_from_node(node)
                if listing and listing.id not in found:
                    found[listing.id] = listing
    return list(found.values())


def listings_from_html(html: str) -> list[RawListing]:
    return listings_from_json_texts(_SCRIPT_RE.findall(html))


_PRICE_LINE = re.compile(r"^(free|gratuit|gratis|[^\w\s]{1,3}\s?\d|\d[\d.,\s]*\s?[^\w\s]{1,3}$|[A-Z]{1,3}\$?\s?\d)", re.I)


def listings_from_links(links: Iterable[dict]) -> list[RawListing]:
    """Fallback: rendered result cards. Each card's text is usually 'price / title / location'."""
    found: dict[str, RawListing] = {}
    for link in links:
        m = _ITEM_RE.search(link.get("href") or "")
        if not m or m.group(1) in found:
            continue
        lines = [ln.strip() for ln in (link.get("text") or "").splitlines() if ln.strip()]
        prices = [ln for ln in lines if _PRICE_LINE.match(ln)]
        rest = [ln for ln in lines if ln not in prices]
        if not rest:
            continue
        price_text = prices[0] if prices else None
        found[m.group(1)] = RawListing(
            id=m.group(1),
            title=rest[0],
            url=item_url(m.group(1)),
            price_text=price_text,
            price_amount=parse_price(price_text),
            location_text=rest[1] if len(rest) > 1 else None,
            image_url=link.get("img") or None,
        )
    return list(found.values())


# --------------------------------------------------------------------------- browser


class FacebookSource(Source):
    name = "facebook"
    # Go slow: a burst of searches is the fastest way to get the account checkpointed.
    delay_range = (20.0, 45.0)

    def __init__(self, storage_state: Path | None, headless: bool = True, scrolls: int = 3):
        self.storage_state = storage_state if storage_state and storage_state.exists() else None
        self.headless = headless
        self.scrolls = scrolls
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure_browser(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise SourceAuthError("SOURCE=facebook needs the 'playwright' package installed.") from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            storage_state=str(self.storage_state) if self.storage_state else None,
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        self._context.route(
            re.compile(r".*\.(mp4|webm|woff2?)(\?.*)?$"), lambda route: route.abort()
        )
        return self._context

    def close(self) -> None:
        try:
            if self._context is not None and self.storage_state is not None:
                # Keep refreshed session cookies for next time.
                self._context.storage_state(path=str(self.storage_state))
        except Exception:  # pragma: no cover - best effort
            log.warning("Could not save refreshed Facebook cookies", exc_info=True)
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # pragma: no cover
                pass
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = None

    def search(self, query: str, area: Area) -> list[RawListing]:
        url = build_search_url(query, area)
        context = self._ensure_browser()
        page = context.new_page()
        graphql_bodies: list[str] = []

        def on_response(resp):
            if "/api/graphql" in resp.url:
                try:
                    graphql_bodies.append(resp.text())
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self._check_blocked(page.url)
            try:
                page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=15000)
            except Exception:
                self._check_blocked(page.url)  # may have redirected late
                # Could also be zero results; carry on and parse whatever is there.
            page.keyboard.press("Escape")  # dismiss a logged-out "log in to see more" dialog
            for _ in range(self.scrolls):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1800)
            self._check_blocked(page.url)

            scripts = page.eval_on_selector_all(
                'script[type="application/json"]', "els => els.map(e => e.textContent)"
            )
            listings = {r.id: r for r in listings_from_json_texts([*scripts, *graphql_bodies])}
            links = page.eval_on_selector_all(
                'a[href*="/marketplace/item/"]',
                "as => as.map(a => ({href: a.getAttribute('href'), text: a.innerText,"
                " img: (a.querySelector('img') || {}).src || null}))",
            )
            for r in listings_from_links(links):
                listings.setdefault(r.id, r)
            return [r for r in listings.values() if not r.is_sold]
        except (SourceError, SourceAuthError):
            raise
        except Exception as exc:
            raise SourceError(f"Facebook page failed to load for {url}: {exc}") from exc
        finally:
            page.close()

    def _check_blocked(self, current_url: str) -> None:
        if "/login" in current_url or "/checkpoint" in current_url:
            hint = (
                "Facebook cookies were rejected or expired; export fresh ones."
                if self.storage_state
                else "No Facebook cookies configured (FB_STORAGE_STATE)."
            )
            raise SourceAuthError(f"Facebook redirected to its login/checkpoint page. {hint}")
