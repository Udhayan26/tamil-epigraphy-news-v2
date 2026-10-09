"""Open each candidate URL, extract the article, and keep it if keywords match."""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import trafilatura

from .config import Config
from .db import DB, now
from .match import Matcher, is_tamil, make_snippet
from . import http
from .sources.util import date_from_url, date_from_ts

log = logging.getLogger(__name__)

MIN_TEXT = 200   # characters; below this the page probably isn't an article
TITLE_TRUST = ("gnews", "archive:")   # collectors whose headline already matched


def _extract(html: str, url: str) -> dict | None:
    try:
        d = trafilatura.bare_extraction(html, url=url, with_metadata=True, favor_recall=True)
    except Exception:
        return None
    if d is None:
        return None
    d = d.as_dict() if hasattr(d, "as_dict") else d
    return {"title": d.get("title") or "", "date": d.get("date") or "", "text": d.get("text") or ""}


def _fetch_archive(url: str, ts: str) -> tuple[dict | None, str]:
    try:
        r = http.get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=60, retries=2, backoff=5)
        if r is not None and r.status_code == 200:
            ex = _extract(r.text, url)
            if ex and len(ex["text"]) >= MIN_TEXT:
                return ex, f"https://web.archive.org/web/{ts}/{url}"
    except Exception:
        pass
    return None, ""


def _fetch(url: str, wayback_ts: str) -> tuple[dict | None, str, str]:
    """Return (extracted, archive_url_used, error)."""
    err = ""
    # 1) live page
    try:
        r = http.get(url, timeout=30, retries=2, backoff=3)
        if r is not None and r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
            ex = _extract(r.text, r.url)
            if ex and len(ex["text"]) >= MIN_TEXT:
                return ex, "", ""
            err = "live page had no article text"
        else:
            err = f"live HTTP {getattr(r, 'status_code', None)}"
    except Exception as e:
        err = f"live error {type(e).__name__}"
    # 2) Wayback copy ('id_' = original page without the archive toolbar)
    ts = wayback_ts or "2"
    arch = f"https://web.archive.org/web/{ts}id_/{url}"
    try:
        r = http.get(arch, timeout=60, retries=2, backoff=5)
        if r is not None and r.status_code == 200:
            ex = _extract(r.text, url)
            if ex and len(ex["text"]) >= MIN_TEXT:
                return ex, f"https://web.archive.org/web/{ts}/{url}", ""
            err += "; archive had no article text"
        else:
            err += f"; archive HTTP {getattr(r, 'status_code', None)}"
    except Exception as e:
        err += f"; archive error {type(e).__name__}"
    return None, "", err


def country_for(domain: str, cfg: Config) -> str:
    d = domain.lower().removeprefix("www.")
    for known, info in cfg.domain_info.items():
        if d == known or d.endswith("." + known):
            return info.get("country", "")
    for tld, c in sorted(cfg.tld_country.items(), key=lambda kv: -len(kv[0])):
        if d.endswith(tld):
            return c
    return "Unknown"


def _process(row, cfg: Config, matcher: Matcher) -> tuple[str, dict | None, str]:
    url = row["url"]
    if "news.google.com" in url:
        # Undecoded Google News link: the page is only a Google redirect, so judge by headline.
        ex, arch, err = None, "", "google news link not decoded"
    else:
        ex, arch, err = _fetch(url, row["wayback_ts"] or "")
    title = (ex or {}).get("title") or row["hint_title"] or ""
    text = (ex or {}).get("text", "")
    basis = "full-text"
    if ex:
        res = matcher.accept(title, text)
        if not res and not arch and row["wayback_ts"]:
            # Live page may be a paywall / cookie screen: try the saved archive copy.
            ex2, arch2 = _fetch_archive(url, row["wayback_ts"])
            if ex2:
                res2 = matcher.accept(ex2["title"] or title, ex2["text"])
                if res2:
                    ex, arch, res = ex2, arch2, res2
                    title, text = ex2["title"] or title, ex2["text"]
        if not res:
            return "rejected", None, ""
    else:
        # Page unreachable: keep it only if the headline itself already matched
        # (Google News / archive headline lists), flagged as title-only.
        if row["hint_title"] and row["collector"].startswith(TITLE_TRUST):
            res = matcher.match(row["hint_title"])
            if not res.ok:
                return "rejected", None, ""
            basis = "title-only"
        else:
            return "failed", None, err
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    pub = (ex or {}).get("date") or row["hint_date"] or date_from_url(url) or ""
    if not pub and row["wayback_ts"]:
        pub = "≤" + (date_from_ts(row["wayback_ts"]) or "")
    year = None
    digits = pub.lstrip("≤")[:4]
    if digits.isdigit():
        year = int(digits)
    art = {
        "url": url,
        "title": title.strip(),
        "pub_date": pub,
        "year": year,
        "domain": domain,
        "source_country": country_for(domain, cfg),
        "language": "Tamil" if is_tamil(title + " " + text[:500]) else "English/Other",
        "groups": json.dumps(res.groups, ensure_ascii=False),
        "matched_terms": json.dumps(res.terms, ensure_ascii=False),
        "places": json.dumps(res.places, ensure_ascii=False),
        "snippet": make_snippet(text, title),
        "collector": row["collector"],
        "archive_url": arch,
        "match_basis": basis,
        "fetched_at": now(),
    }
    return "matched", art, ""


def verify_batch(db: DB, cfg: Config, matcher: Matcher, batch: int = 120,
                 workers: int = 6) -> int:
    rows = db.pending(batch)
    if not rows:
        return 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda r: _process(r, cfg, matcher), rows))
    kept = 0
    for row, (status, art, err) in zip(rows, results):
        db.mark(row["url"], status, err)
        if art:
            db.save_article(art)
            kept += 1
    db.commit()
    log.info("verified %d urls: %d kept", len(rows), kept)
    return len(rows)


def verify_until(db: DB, cfg: Config, matcher: Matcher, deadline: float) -> None:
    while time.time() < deadline:
        if verify_batch(db, cfg, matcher) == 0:
            break
