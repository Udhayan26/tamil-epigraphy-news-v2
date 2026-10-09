"""Backfill from the Internet Archive's Wayback Machine URL index (CDX API).

For each news domain we ask the CDX server for every archived URL whose address
matches our slug terms (e.g. 'hero-stone', 'inscription', 'keeladi', or a
percent-encoded Tamil word). This reaches back to the mid-2000s for most sites.
Progress is saved per domain/term-batch/page so runs can resume.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ..config import Config
from ..db import DB
from .. import http
from .util import date_from_url

log = logging.getLogger(__name__)

CDX = "https://web.archive.org/cdx/search/cdx"
# Short regexes keep each archive query fast; big sites time out on long ones.
MAX_REGEX_LEN = 350

SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|css|js|pdf|mp4|mp3|xml|json|rss|ico)(\?|$)", re.I)
SKIP_PATH = re.compile(r"/(tag|tags|topic|topics|search|author|authors|amp/amp|photos?|gallery|videos?|comments?)/", re.I)


def _batches(terms: list[str]) -> list[str]:
    """Split terms into regex alternations short enough for a GET request."""
    out, cur = [], []
    for t in terms:
        esc = re.escape(t).replace("\\-", "-").replace("\\%", "%")
        if cur and len("|".join(cur + [esc])) > MAX_REGEX_LEN:
            out.append("|".join(cur))
            cur = []
        cur.append(esc)
    if cur:
        out.append("|".join(cur))
    return out


def plan(cfg: Config) -> list[dict]:
    """List of (domain, regex) jobs."""
    en_terms = [t for t in cfg.url_slug_terms if "%" not in t]
    ta_terms = [t for t in cfg.url_slug_terms if "%" in t]
    jobs = []
    for d in cfg.wayback.get("domains", []):
        terms = list(en_terms)
        if d.get("lang") == "ta":
            terms += ta_terms
        for i, rx in enumerate(_batches(terms)):
            jobs.append({"domain": d["domain"], "path_prefix": d.get("path_prefix"),
                         "batch": i, "regex": rx})
    return jobs


def _params(job: dict, cfg: Config) -> dict:
    w = cfg.wayback
    url = job["path_prefix"] + "*" if job.get("path_prefix") else job["domain"]
    p = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original",
        "collapse": "urlkey",
        "from": str(w.get("from_year", 2005)),
        "to": str(w.get("to_year", 2026)),
        "filter": ["statuscode:200", "mimetype:text/html", f"original:(?i).*({job['regex']}).*"],
    }
    if not job.get("path_prefix"):
        p["matchType"] = "domain"
    return p


def _num_pages(job: dict, cfg: Config) -> int:
    p = _params(job, cfg)
    p.pop("output", None)
    p["showNumPages"] = "true"
    r = http.get(CDX, params=p, timeout=90, retries=2)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"CDX numpages HTTP {getattr(r, 'status_code', None)}")
    try:
        return int(r.text.strip())
    except ValueError:
        return 1


def step(db: DB, cfg: Config) -> bool:
    """Process one CDX page. Returns False when every job is finished."""
    jobs = plan(cfg)
    st = db.get_state("wayback", {"job": 0, "page": 0, "pages": None})
    # If keywords/domains changed, job list changes; we just continue by index.
    if st["job"] >= len(jobs):
        return False
    job = jobs[st["job"]]
    try:
        if st["pages"] is None:
            st["pages"] = _num_pages(job, cfg)
            db.set_state("wayback", st)
        if st["page"] >= st["pages"]:
            db.set_state("wayback", {"job": st["job"] + 1, "page": 0, "pages": None})
            return True

        p = _params(job, cfg)
        p["page"] = str(st["page"])
        r = http.get(CDX, params=p, timeout=120, retries=2)
        if r is None or r.status_code != 200:
            raise RuntimeError(f"CDX HTTP {getattr(r, 'status_code', None)}")
        text = r.text.strip()
        rows = json.loads(text) if text else []
        cands = []
        for row in rows[1:] if rows and rows[0] == ["timestamp", "original"] else rows:
            ts, url = row[0], row[1]
            if SKIP_EXT.search(url) or SKIP_PATH.search(url):
                continue
            url = url.replace("http://", "https://", 1) if url.startswith("http://") else url
            cands.append({"url": url, "collector": f"wayback:{job['domain']}",
                          "date": date_from_url(url) or "", "wayback_ts": ts})
        added = db.add_candidates(cands)
        log.info("wayback %s batch %d page %d/%d: %d urls, %d new",
                 job["domain"], job["batch"], st["page"] + 1, st["pages"], len(cands), added)
        st["page"] += 1
        st.pop("errors", None)
        db.set_state("wayback", st)
    except Exception as e:  # network trouble: retry this page a few times, then skip it
        st["errors"] = st.get("errors", 0) + 1
        log.warning("wayback %s page %s error (%d): %s", job["domain"], st["page"], st["errors"], e)
        if st["errors"] >= 3:
            skipped = db.get_state("wayback_skipped", [])
            skipped.append({"domain": job["domain"], "batch": job["batch"], "page": st["page"]})
            db.set_state("wayback_skipped", skipped)
            st["errors"] = 0
            if st["pages"] is None:
                st = {"job": st["job"] + 1, "page": 0, "pages": None}
            else:
                st["page"] += 1
        db.set_state("wayback", st)
        time.sleep(5)
    return True


def progress(db: DB, cfg: Config) -> str:
    jobs = plan(cfg)
    st = db.get_state("wayback", {"job": 0, "page": 0, "pages": None})
    if st["job"] >= len(jobs):
        return f"wayback: done ({len(jobs)} domain jobs)"
    return (f"wayback: job {st['job'] + 1}/{len(jobs)} ({jobs[st['job']]['domain']}), "
            f"page {st['page']}/{st['pages']}")
