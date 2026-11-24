"""Turn 'Dallas, TX' into coordinates using OpenStreetMap's Nominatim (free, 1 request/sec)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "faraway-finds/0.1 (self-hosted community app)"


@dataclass
class Place:
    lat: float
    lng: float
    label: str  # city-level, safe to show to other users


class GeocodeError(Exception):
    pass


def _label(result: dict) -> str:
    addr = result.get("address") or {}
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("suburb")
        or addr.get("county")
    )
    region = addr.get("state") or addr.get("region")
    country = addr.get("country")
    parts = [p for p in (city, region, country) if p]
    if parts:
        return ", ".join(dict.fromkeys(parts))
    return ", ".join(result.get("display_name", "").split(", ")[:3])


def geocode(query: str, client: httpx.Client | None = None) -> Place:
    query = query.strip()
    if not query:
        raise GeocodeError("Enter a city or postal code.")
    own = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.get(
            NOMINATIM,
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
        )
        resp.raise_for_status()
        results = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GeocodeError(f"Couldn't reach the location service ({exc}). Try again shortly.") from exc
    finally:
        if own:
            client.close()
    if not results:
        raise GeocodeError(f"Couldn't find “{query}”. Try a city name, e.g. “Dallas, TX”.")
    top = results[0]
    return Place(lat=round(float(top["lat"]), 4), lng=round(float(top["lon"]), 4), label=_label(top))
