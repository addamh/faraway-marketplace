"""Fake listings so the app can be tried end to end without touching Facebook."""

from __future__ import annotations

import hashlib
import random
from datetime import date

from .base import Area, RawListing, Source, item_url

_VARIANTS = [
    "{q}",
    "{q} - great condition",
    "{q} (complete in box)",
    "{q} cartridge only",
    "Lot of 5 incl. {q}",
    "Vintage {q}",
    "{q} + extras, must go",
]
_JUNK = ["Phone case", "Kids bike", "Box of cables", "Desk lamp", "Poster frame", "Board game bundle"]


class DemoSource(Source):
    name = "demo"

    def search(self, query: str, area: Area) -> list[RawListing]:
        city = (area.label or "Nearby").split(",")[0]
        seed = f"{query.lower()}|{area.lat:.2f}|{area.lng:.2f}"
        rng = random.Random(seed)
        # A couple of new listings "appear" every day.
        day_rng = random.Random(seed + date.today().isoformat())
        count = rng.randint(2, 5) + day_rng.randint(0, 2)
        currency = "€" if -30 < area.lng < 45 and area.lat > 34 else "$"
        out: list[RawListing] = []
        for i in range(count):
            ident = int(hashlib.sha1(f"{seed}|{i}|{date.today().toordinal() // 3}".encode()).hexdigest()[:12], 16)
            listing_id = str(100000000000000 + ident % 899999999999999)
            if rng.random() < 0.25:
                title = rng.choice(_JUNK)
            else:
                title = rng.choice(_VARIANTS).format(q=query.title())
            amount = rng.choice([0, 5, 12, 15, 20, 25, 35, 40, 60, 85, 120])
            price_text = "Free" if amount == 0 else (f"{amount} €" if currency == "€" else f"${amount}")
            out.append(
                RawListing(
                    id=listing_id,
                    title=title,
                    url=item_url(listing_id),
                    price_text=price_text,
                    price_amount=float(amount),
                    location_text=city,
                    image_url=f"/demo-image/{listing_id}.svg",
                )
            )
        return out
