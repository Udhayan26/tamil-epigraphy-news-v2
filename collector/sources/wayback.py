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


PAGE_LIMIT = 3000      # rows per request
STATE = "wayback_v2"   # v2: resume-key paging (page-number paging returned nothing with filters)


def parse_cdx(text: str) -> tuple[list[list[str]], str | None]:
    """Parse CDX JSON output. With showResumeKey the last rows are [] and [key]."""
    text = (text or "").strip()
    if not text:
        return [], None
    rows = json.loads(text)
    if not isinstance(rows, list):
        return [], None
    resume = None
    if len(rows) >= 2 and rows[-2] == [] and isinstance(rows[-1], list) and len(rows[-1]) == 1:
        resume = rows[-1][0]
        rows = rows[:-2]
    if rows and rows[0] == ["timestamp", "original"]:
        rows = rows[1:]
    return [r for r in rows if isinstance(r, list) and len(r) >= 2], resume


def _bump_count(db: DB, domain: str, n: int) -> None:
    counts = db.get_state("wayback_counts", {})
    counts[domain] = counts.get(domain, 0) + n
    db.set_state("wayback_counts", counts)


def step(db: DB, cfg: Config) -> bool:
    """Fetch the next block of archived URLs. Returns False when every job is finished."""
    jobs = plan(cfg)
    st = db.get_state(STATE, {"job": 0, "resume": None})
    if st["job"] >= len(jobs):
        return False
    job = jobs[st["job"]]
    try:
        p = _params(job, cfg)
        p["limit"] = str(PAGE_LIMIT)
        p["showResumeKey"] = "true"
        if st.get("resume"):
            p["resumeKey"] = st["resume"]
        r = http.get(CDX, params=p, timeout=150, retries=2)
        if r is None or r.status_code != 200:
            raise RuntimeError(f"CDX HTTP {getattr(r, 'status_code', None)}")
        rows, resume = parse_cdx(r.text)
        cands = []
        for ts, url, *_ in rows:
            if SKIP_EXT.search(url) or SKIP_PATH.search(url):
                continue
            url = url.replace("http://", "https://", 1) if url.startswith("http://") else url
            cands.append({"url": url, "collector": f"wayback:{job['domain']}",
                          "date": date_from_url(url) or "", "wayback_ts": ts})
        added = db.add_candidates(cands)
        _bump_count(db, job["domain"], len(cands))
        log.info("wayback %s batch %d: %d urls, %d new%s", job["domain"], job["batch"],
                 len(cands), added, " (more)" if resume else "")
        st = ({"job": st["job"], "resume": resume} if resume
              else {"job": st["job"] + 1, "resume": None})
    except Exception as e:  # network trouble: retry a few times, then skip this job
        st["errors"] = st.get("errors", 0) + 1
        log.warning("wayback %s error (%d): %s", job["domain"], st["errors"], e)
        if st["errors"] >= 3:
            skipped = db.get_state("wayback_skipped", [])
            skipped.append({"domain": job["domain"], "batch": job["batch"], "error": str(e)[:120]})
            db.set_state("wayback_skipped", skipped)
            st = {"job": st["job"] + 1, "resume": None}
        time.sleep(5)
    db.set_state(STATE, st)
    return True


def progress(db: DB, cfg: Config) -> str:
    jobs = plan(cfg)
    st = db.get_state(STATE, {"job": 0, "resume": None})
    counts = db.get_state("wayback_counts", {})
    found = sum(counts.values())
    skipped = len(db.get_state("wayback_skipped", []))
    head = (f"wayback: done ({len(jobs)} jobs)" if st["job"] >= len(jobs) else
            f"wayback: job {st['job'] + 1}/{len(jobs)} ({jobs[st['job']]['domain']})")
    top = ", ".join(f"{d} {n}" for d, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6])
    return f"{head}; links found so far {found}; jobs skipped {skipped}" + (f"; top: {top}" if top else "")
