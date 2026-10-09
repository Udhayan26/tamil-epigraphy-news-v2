"""Newspaper "archive by date" pages (e.g. The Hindu print archive).

Each day's index page lists that day's headlines. Headlines matching a keyword
are queued for verification. Progress is saved per archive as the last date done.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from urllib.parse import urljoin

import lxml.html

from ..config import Config
from ..db import DB
from ..match import Matcher
from .. import http

log = logging.getLogger(__name__)


def _parse_links(html: str, base: str) -> list[tuple[str, str]]:
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    out = []
    for a in doc.iter("a"):
        href = a.get("href")
        text = " ".join(a.text_content().split())
        if href and len(text) > 15:
            out.append((urljoin(base, href), text))
    return out


def step(db: DB, cfg: Config, matcher: Matcher, days_per_step: int = 1) -> bool:
    """Process the next day of the first unfinished archive. False when all done."""
    for arc in cfg.date_archives:
        key = f"date_archive:{arc['name']}"
        st = db.get_state(key, {"next": arc["from"], "empty_streak": 0})
        nxt = date.fromisoformat(st["next"])
        end = min(date.fromisoformat(arc["to"]), date.today())
        if nxt > end:
            continue
        # If the site seems not to work at all (many empty days in a row from the start), give up.
        if st.get("empty_streak", 0) >= 60 and st.get("ever_found", 0) == 0:
            continue
        for _ in range(days_per_step):
            if nxt > end:
                break
            url = arc["url"].format(y=nxt.year, m=f"{nxt.month:02d}", d=f"{nxt.day:02d}")
            try:
                r = http.get(url, timeout=40, retries=2)
                links = _parse_links(r.text, url) if r is not None and r.status_code == 200 else []
            except Exception as e:
                log.warning("archive %s %s: %s", arc["name"], nxt, e)
                links = []
            must = arc.get("link_must_contain", "")
            cands = [{"url": u, "title": t, "date": nxt.isoformat(),
                      "collector": f"archive:{arc['name']}"}
                     for u, t in links if (not must or must in u) and matcher.title_candidate(t)]
            added = db.add_candidates(cands)
            if links:
                st["empty_streak"] = 0
                st["ever_found"] = st.get("ever_found", 0) + 1
            else:
                st["empty_streak"] = st.get("empty_streak", 0) + 1
            if cands:
                log.info("archive %s %s: %d headline matches, %d new", arc["name"], nxt, len(cands), added)
            nxt += timedelta(days=1)
        st["next"] = nxt.isoformat()
        db.set_state(key, st)
        return True
    return False


def progress(db: DB, cfg: Config) -> str:
    parts = []
    for arc in cfg.date_archives:
        st = db.get_state(f"date_archive:{arc['name']}", {"next": arc["from"]})
        parts.append(f"{arc['name']}: next {st['next']}")
    return "archives: " + "; ".join(parts) if parts else "archives: none"
