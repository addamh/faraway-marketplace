from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    """Where to search: a helper's pickup area."""

    lat: float
    lng: float
    radius_km: float
    label: str = ""
    fb_location: str | None = None  # facebook source only


@dataclass
class RawListing:
    id: str
    title: str
    url: str
    price_text: str | None = None
    price_amount: float | None = None
    location_text: str | None = None
    image_url: str | None = None
    is_sold: bool = False
    is_pending: bool = False


class SourceError(Exception):
    """A single search failed; the run carries on with the next one."""


class SourceAuthError(SourceError):
    """The source can't work at all until someone fixes credentials. The run stops early."""


class Source:
    name = "base"
    # Seconds to wait between requests (min, max). Real scrapers should go slow.
    delay_range: tuple[float, float] = (0.0, 0.0)

    def __enter__(self) -> Source:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        pass

    def search(self, query: str, area: Area) -> list[RawListing]:
        raise NotImplementedError


_NUM_RE = re.compile(r"\d[\d.,\s  ]*")


def parse_price(text: str | None) -> float | None:
    """'$1,200' -> 1200.0, '25 €' -> 25.0, '1.234,50 €' -> 1234.5, 'Free' -> 0.0."""
    if not text:
        return None
    if text.strip().lower() in {"free", "gratuit", "gratis", "kostenlos"}:
        return 0.0
    m = _NUM_RE.search(text)
    if not m:
        return None
    num = re.sub(r"[\s  ]", "", m.group(0)).rstrip(".,")
    if "," in num and "." in num:
        # Whichever separator comes last is the decimal point.
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = num.replace(",", "") if len(tail) == 3 else f"{head.replace(',', '')}.{tail}"
    elif num.count(".") > 1 or (num.count(".") == 1 and len(num.rpartition(".")[2]) == 3):
        num = num.replace(".", "")  # European thousands separator
    try:
        return float(num)
    except ValueError:
        return None


def item_url(listing_id: str) -> str:
    return f"https://www.facebook.com/marketplace/item/{listing_id}/"
