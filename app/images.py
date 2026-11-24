"""Cache listing thumbnails locally: Facebook's image URLs are signed and expire within days."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
MAX_BYTES = 3 * 1024 * 1024
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def cache_image(client: httpx.Client, listing_id: str, url: str | None, images_dir: Path) -> str | None:
    """Download url to images_dir; return the file name, or None if it can't be fetched."""
    if not url or not url.startswith("http"):
        return None
    try:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("thumbnail fetch failed for %s: %s", listing_id, exc)
        return None
    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if ctype not in _EXT or len(resp.content) > MAX_BYTES:
        return None
    safe_id = "".join(c for c in listing_id if c.isalnum())
    name = f"{safe_id}{_EXT[ctype]}"
    (images_dir / name).write_bytes(resp.content)
    return name
