"""Decide whether a listing is a real hit for a search.

Marketplace search is fuzzy: "gameboy mega man" also returns phone cases and Xbox games.
These filters run on our side, after fetching, so several searches can share one fetch.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Search
from .sources.base import RawListing


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _squash(text: str) -> str:
    return _fold(text).replace(" ", "")


def split_terms(raw: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n]", raw or "") if t.strip()]


def contains_term(title: str, term: str) -> bool:
    """Word-order and spacing tolerant: 'mega man' matches 'MegaMan' and 'Mega-Man'."""
    needle = _squash(term)
    return bool(needle) and needle in _squash(title)


def matches(search: Search, listing: RawListing) -> bool:
    title = listing.title or ""
    if listing.is_sold:
        return False
    if search.strict:
        for word in _fold(search.query).split():
            if not contains_term(title, word):
                return False
    for term in split_terms(search.must_include):
        if not contains_term(title, term):
            return False
    for term in split_terms(search.exclude):
        if contains_term(title, term):
            return False
    price = listing.price_amount
    if price is not None:
        if search.min_price is not None and price < search.min_price:
            return False
        if search.max_price is not None and price > search.max_price:
            return False
    return True


def normalize_query(query: str) -> str:
    return " ".join(_fold(query).split())
