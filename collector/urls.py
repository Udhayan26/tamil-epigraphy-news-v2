"""Address clean-up shared by all collectors."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# Second addresses that serve the same article as the main site.
HOST_ALIASES = {
    "image.nakkheeran.in": "www.nakkheeran.in",
    "nakkheeran.in": "www.nakkheeran.in",
}


def tidy(url: str) -> str:
    """https, no default port, no fragment, known mirror hosts mapped to the main site.
    Query strings are kept (some sites use ?id= for the article)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if parts.scheme not in ("http", "https"):
        return url
    host = (parts.hostname or "").lower()
    host = HOST_ALIASES.get(host, host)
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
