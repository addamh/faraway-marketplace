from __future__ import annotations

from ..config import Settings
from .base import Area, RawListing, Source, SourceAuthError, SourceError

__all__ = ["Area", "RawListing", "Source", "SourceAuthError", "SourceError", "get_source"]


def get_source(settings: Settings) -> Source:
    name = settings.source
    if name == "demo":
        from .demo import DemoSource

        return DemoSource()
    if name == "serpapi":
        from .serpapi import SerpApiSource

        return SerpApiSource(settings.serpapi_key)
    if name == "facebook":
        from .facebook import FacebookSource

        return FacebookSource(settings.fb_storage_state, headless=settings.fb_headless)
    raise ValueError(f"Unknown SOURCE={name!r}; expected demo, serpapi or facebook")
