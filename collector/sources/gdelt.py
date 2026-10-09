"""GDELT DOC 2.0 API: worldwide online news, searchable from 2017 onward.

Backfill walks each search query month by month; daily mode searches the last
few days. GDELT asks for at most one request every 5 seconds.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from ..config import Config
from ..db import DB
from .. import http

log = logging.getLogger(__name__)

API = "https://api.gdeltproject.org/api/v2/doc/doc"
START = date(2017, 1, 1)


def _month_windows(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month // 12), cur.month % 12 + 1, 1)
        out.append((cur, min(nxt - timedelta(days=1), end)))
        cur = nxt
    return out


def _queries(cfg: Config) -> list[str]:
    return list(cfg.search_queries_en) + list(cfg.search_queries_ta)


def search(query: str, start: datetime, end: datetime, *, retries: int = 3,
           timeout: float = 60) -> list[dict]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": "250",
        "sort": "DateAsc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    r = http.get(API, params=params, timeout=timeout, retries=retries, backoff=3)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"GDELT HTTP {getattr(r, 'status_code', None)}")
    txt = r.text.strip()
    if not txt.startswith("{"):
        # GDELT returns plain-text messages for bad/too-short queries
        log.info("GDELT note for %r: %s", query, txt[:120])
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return []
    rows = []
    for a in data.get("articles", []) or []:
        seen = a.get("seendate", "")
        d = f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}" if len(seen) >= 8 else ""
        rows.append({"url": a.get("url"), "title": a.get("title", ""), "date": d,
                     "collector": "gdelt"})
    return [r for r in rows if r["url"]]


def step(db: DB, cfg: Config) -> bool:
    """One query x one month. Returns False when the backfill is finished."""
    queries = _queries(cfg)
    windows = _month_windows(START, date.today())
    st = db.get_state("gdelt", {"q": 0, "w": 0})
    if st["q"] >= len(queries):
        return False
    q, (ws, we) = queries[st["q"]], windows[min(st["w"], len(windows) - 1)]
    try:
        rows = search(q, datetime(ws.year, ws.month, ws.day),
                      datetime(we.year, we.month, we.day, 23, 59, 59))
        added = db.add_candidates(rows)
        if rows:
            log.info("gdelt %r %s: %d results, %d new", q, ws.strftime("%Y-%m"), len(rows), added)
        st.pop("errors", None)
        st["w"] += 1
    except Exception as e:
        st["errors"] = st.get("errors", 0) + 1
        log.warning("gdelt %r %s error: %s", q, ws, e)
        if st["errors"] >= 3:
            st["w"] += 1
            st["errors"] = 0
    if st["w"] >= len(windows):
        st = {"q": st["q"] + 1, "w": 0}
    db.set_state("gdelt", st)
    return True


def recent(db: DB, cfg: Config, days: int = 3, budget_min: float = 8) -> int:
    """Latest days only. Stops at the time budget or after repeated failures
    (GDELT throttles busy clients), so it can never stall a whole run."""
    import time as _t
    stop_at = _t.time() + budget_min * 60
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days)
    total, fails = 0, 0
    for q in _queries(cfg):
        if _t.time() > stop_at:
            log.info("gdelt recent: time budget reached")
            break
        try:
            total += db.add_candidates(search(q, start, end, retries=1, timeout=25))
            fails = 0
        except Exception as e:
            fails += 1
            log.warning("gdelt recent %r: %s", q, e)
            if fails >= 3:
                log.warning("gdelt recent: 3 failures in a row, skipping the rest this run")
                break
    return total


def progress(db: DB, cfg: Config) -> str:
    queries = _queries(cfg)
    st = db.get_state("gdelt", {"q": 0, "w": 0})
    if st["q"] >= len(queries):
        return f"gdelt: done ({len(queries)} queries)"
    return f"gdelt: query {st['q'] + 1}/{len(queries)} ({queries[st['q']]}), month {st['w']}"
