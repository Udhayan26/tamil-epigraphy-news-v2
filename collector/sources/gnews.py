"""Google News RSS search (recent articles, Tamil and English editions)."""
from __future__ import annotations

import base64
import logging
import re
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import feedparser

from ..config import Config
from ..db import DB
from .. import http

log = logging.getLogger(__name__)

FEEDS = {
    "en": "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",
    "ta": "https://news.google.com/rss/search?q={q}&hl=ta&gl=IN&ceid=IN:ta",
}
URL_IN_BYTES = re.compile(rb"https?://[\x21-\x7e]+")


def decode_gnews(link: str) -> str:
    """Best effort: recover the publisher URL from a news.google.com article link.

    Older Google News ids are base64 protobufs that contain the URL directly.
    Newer ids cannot be decoded offline; for those we try following redirects,
    and otherwise keep the Google link (it still opens the article in a browser).
    """
    if "news.google.com" not in link:
        return link
    # Current Google News ids need an online lookup; the googlenewsdecoder package does it.
    try:
        from googlenewsdecoder import gnewsdecoder
        res = gnewsdecoder(link, timeout=15)
        if isinstance(res, dict) and res.get("success") and res.get("decoded_url"):
            return res["decoded_url"]
    except Exception:
        pass
    m = re.search(r"/articles/([A-Za-z0-9_\-]+)", link)
    if m:
        s = m.group(1)
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            u = URL_IN_BYTES.search(raw)
            if u:
                return u.group(0).decode("ascii", "ignore").rstrip("\x00")
        except Exception:
            pass
    try:
        r = http.get(link, timeout=20, retries=1)
        if r is not None and "news.google.com" not in urlparse(r.url).netloc:
            return r.url
    except Exception:
        pass
    return link


def fetch(cfg: Config, recent_days: int | None = 7, budget_min: float = 10) -> list[dict]:
    import time as _t
    stop_at = _t.time() + budget_min * 60
    from ..match import Matcher
    matcher = Matcher(cfg)
    seen: set[str] = set()
    rows = []
    jobs = [(q, "en") for q in cfg.search_queries_en] + [(q, "ta") for q in cfg.search_queries_ta]
    for q, lang in jobs:
        if _t.time() > stop_at:
            log.info("gnews: time budget reached")
            break
        query = q + (f" when:{recent_days}d" if recent_days else "")
        url = FEEDS[lang].format(q=quote_plus(query))
        try:
            r = http.get(url, timeout=30, retries=2, backoff=3)
            if r is None or r.status_code != 200:
                continue
            feed = feedparser.parse(r.content)
        except Exception as e:
            log.warning("gnews %r: %s", q, e)
            continue
        for e in feed.entries:
            title = e.get("title", "")
            # Google appends " - Publisher" to titles
            src = (e.get("source") or {}).get("title", "")
            if src and title.endswith(" - " + src):
                title = title[: -len(src) - 3]
            d = ""
            if e.get("published"):
                try:
                    d = parsedate_to_datetime(e.published).date().isoformat()
                except Exception:
                    pass
            link = e.get("link", "")
            # Only headlines that mention a keyword are worth decoding and checking.
            if not link or link in seen or not matcher.title_candidate(title):
                continue
            seen.add(link)
            rows.append({"url": decode_gnews(link), "title": title, "date": d,
                         "collector": "gnews"})
    return [r for r in rows if r["url"]]


def recent(db: DB, cfg: Config, days: int = 7, budget_min: float = 10) -> int:
    return db.add_candidates(fetch(cfg, days, budget_min))
