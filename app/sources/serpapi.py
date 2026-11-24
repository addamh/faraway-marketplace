"""SerpApi's Facebook Marketplace engine: a paid API that takes lat/lon + radius directly.

Docs: https://serpapi.com/facebook-marketplace-api
"""

from __future__ import annotations

import httpx

from .base import Area, RawListing, Source, SourceAuthError, SourceError, item_url, parse_price

ENDPOINT = "https://serpapi.com/search.json"


class SerpApiSource(Source):
    name = "serpapi"
    delay_range = (0.5, 1.5)

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise SourceAuthError("SOURCE=serpapi needs SERPAPI_KEY to be set.")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=60)

    def close(self) -> None:
        self.client.close()

    def search(self, query: str, area: Area) -> list[RawListing]:
        params = {
            "engine": "facebook_marketplace",
            "q": query,
            "lat": f"{area.lat:.5f}",
            "lon": f"{area.lng:.5f}",
            "radius": str(max(1, round(area.radius_km))),
            "api_key": self.api_key,
        }
        try:
            resp = self.client.get(ENDPOINT, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"SerpApi request failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"SerpApi returned HTTP {resp.status_code} with a non-JSON body") from exc

        error = data.get("error") if isinstance(data, dict) else None
        if error:
            if "hasn't returned any results" in error or "no results" in error.lower():
                return []
            if resp.status_code in (401, 403) or "api key" in error.lower():
                raise SourceAuthError(f"SerpApi rejected the API key: {error}")
            raise SourceError(f"SerpApi error: {error}")
        if resp.status_code >= 400:
            raise SourceError(f"SerpApi returned HTTP {resp.status_code}")

        out = []
        # Only organic_results: relaxed_results are listings outside the requested area.
        for r in data.get("organic_results") or []:
            listing_id = str(r.get("listing_id") or "").strip()
            title = (r.get("title") or r.get("name") or "").strip()
            if not listing_id or not title:
                continue
            price_text = r.get("price")
            amount = r.get("extracted_price")
            out.append(
                RawListing(
                    id=listing_id,
                    title=title,
                    url=r.get("link") or item_url(listing_id),
                    price_text=price_text,
                    price_amount=float(amount) if isinstance(amount, (int, float)) else parse_price(price_text),
                    location_text=r.get("location"),
                    image_url=r.get("thumbnail"),
                    is_sold=bool(r.get("is_sold")),
                    is_pending=bool(r.get("is_pending")),
                )
            )
        return out
