"""Command line entry point.

    python -m collector run --minutes 300        # scheduled job: today's news + history
    python -m collector backfill --minutes 320   # 20-year history, resumable
    python -m collector daily                    # new articles from the last few days
    python -m collector verify --minutes 60      # just check queued URLs
    python -m collector export                   # rebuild data/articles.xlsx + .csv
    python -m collector status                   # progress report
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import DATA_DIR, load_config
from .db import DB
from .export import export
from .match import Matcher
from .sources import date_archive, gdelt, gnews, papers, wayback
from .verify import verify_batch, verify_until

log = logging.getLogger("collector")

# Keep the verification queue short so results appear steadily during long runs.
QUEUE_HIGH = 300


def status(db: DB, cfg) -> str:
    s = db.stats()
    lines = [
        f"articles kept: {s['matched']}",
        f"urls checked: {s['candidates'] - s['pending']} of {s['candidates']} "
        f"(rejected {s['rejected']}, unreachable {s['failed']}, waiting {s['pending']})",
        wayback.progress(db, cfg),
        date_archive.progress(db, cfg),
        gdelt.progress(db, cfg),
        papers.progress(db, cfg),
    ]
    return "\n".join(lines)


def add_seed_urls(db: DB) -> int:
    """Queue any URLs listed in config/seed_urls.txt (already-known ones are skipped)."""
    from .config import CONFIG_DIR
    p = CONFIG_DIR / "seed_urls.txt"
    if not p.exists():
        return 0
    rows = [{"url": line.strip(), "collector": "seed"}
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("http")]
    return db.add_candidates(rows)


def save(db: DB, cfg) -> None:
    """Write the spreadsheet plus a plain-text progress report (data/status.txt)."""
    res = export(db, cfg, DATA_DIR)
    txt = (time.strftime("Last updated: %Y-%m-%d %H:%M UTC\n", time.gmtime())
           + f"new articles today: {res['new_today']}   |   last 7 days: {res['new_week']}"
           + f"   |   research papers: {res['papers']}\n"
           + status(db, cfg))
    errs = db.conn.execute(
        "SELECT last_error, COUNT(*) n FROM candidates WHERE status='failed' "
        "GROUP BY last_error ORDER BY n DESC LIMIT 5").fetchall()
    if errs:
        txt += "\n\nMost common reasons pages could not be opened:\n" + "\n".join(
            f"  {r['n']:>6}  {r['last_error']}" for r in errs)
    (DATA_DIR / "status.txt").write_text(txt + "\n", encoding="utf-8")


def run_backfill(db: DB, cfg, matcher: Matcher, minutes: float) -> None:
    start = time.time()
    deadline = start + minutes * 60
    # Collect new links during the first 75% of the run; the rest is kept for
    # opening and checking the links already found.
    collect_until = start + minutes * 60 * 0.75
    add_seed_urls(db)
    verify_batch(db, cfg, matcher)          # check seed / leftover links straight away
    collectors = [
        ("wayback", lambda: wayback.step(db, cfg)),
        ("archives", lambda: date_archive.step(db, cfg, matcher, days_per_step=3)),
        ("gdelt", lambda: gdelt.step(db, cfg)),
        ("papers", lambda: papers.openalex_step(db, cfg, matcher)),
        ("journals", lambda: papers.oai_step(db, cfg, matcher)),
    ]
    active = {name for name, _ in collectors}
    i = 0
    last_export = time.time()
    while time.time() < deadline:
        collecting = active and time.time() < collect_until
        if not collecting or db.count_pending() >= QUEUE_HIGH:
            if verify_batch(db, cfg, matcher) == 0 and not collecting:
                if not active:
                    break
                time.sleep(5)   # nothing left to check; wait for the deadline cheaply
                if time.time() >= collect_until:
                    break
        else:
            name, fn = collectors[i % len(collectors)]
            i += 1
            if name in active and not fn():
                log.info("%s collector finished", name)
                active.discard(name)
            if not active:
                db.set_state("backfill_done", True)
            # Check links in small batches as they arrive, so results build up steadily.
            if i % 3 == 0 and db.count_pending() > 0:
                verify_batch(db, cfg, matcher, batch=60)
        # Save a fresh spreadsheet every 30 minutes so partial results are usable.
        if time.time() - last_export > 1800:
            save(db, cfg)
            last_export = time.time()
    save(db, cfg)


def search_recent(db: DB, cfg, budget_min: float = 18) -> None:
    """Latest news from Google News (Tamil + English) and GDELT, within a time budget."""
    try:
        n1 = gnews.recent(db, cfg, days=7, budget_min=budget_min * 0.55)
    except Exception as e:
        log.warning("Google News search failed: %s", e)
        n1 = 0
    try:
        n2 = gdelt.recent(db, cfg, days=3, budget_min=budget_min * 0.45)
    except Exception as e:
        log.warning("GDELT search failed: %s", e)
        n2 = 0
    log.info("recent news: %d new links from Google News, %d from GDELT", n1, n2)


def search_recent_papers(db: DB, cfg, matcher: Matcher, budget_min: float = 8) -> None:
    """New research papers (OpenAlex) and new issues of the Tamil journals."""
    try:
        n = papers.openalex_recent(db, cfg, matcher, days=10, budget_min=budget_min * 0.6)
        log.info("recent papers: %d kept", n)
    except Exception as e:
        log.warning("OpenAlex search failed: %s", e)
    stop_at = time.time() + budget_min * 0.4 * 60
    try:
        while time.time() < stop_at and papers.oai_step(db, cfg, matcher):
            pass
    except Exception as e:
        log.warning("journal harvest failed: %s", e)


def run_daily(db: DB, cfg, matcher: Matcher, minutes: float) -> None:
    deadline = time.time() + minutes * 60
    add_seed_urls(db)
    search_recent(db, cfg)
    verify_until(db, cfg, matcher, deadline)
    save(db, cfg)


def run_all(db: DB, cfg, matcher: Matcher, minutes: float) -> None:
    """Scheduled job: today's news first (checked straight away), then history."""
    start = time.time()
    add_seed_urls(db)
    search_recent(db, cfg, budget_min=min(18, minutes * 0.35))
    search_recent_papers(db, cfg, matcher, budget_min=min(8, minutes * 0.15))
    # Always give the fresh news some checking time (counted from now, not from start).
    verify_until(db, cfg, matcher, time.time() + max(5, min(15, minutes * 0.3)) * 60)
    remaining = minutes - (time.time() - start) / 60
    if remaining > 5 and not db.get_state("backfill_done", False):
        run_backfill(db, cfg, matcher, remaining)
    else:
        verify_until(db, cfg, matcher, start + minutes * 60)
        save(db, cfg)


def migrate(db: DB) -> None:
    """One-time fixes to data saved by earlier versions."""
    if not db.get_state("migr_gnews_recheck"):
        # v2.0 judged Google News links by Google's redirect page and wrongly rejected them.
        n = db.conn.execute(
            "UPDATE candidates SET status='pending', tries=0, last_error=NULL "
            "WHERE collector='gnews' AND status IN ('rejected','failed')").rowcount
        db.conn.commit()
        db.set_state("migr_gnews_recheck", True)
        log.info("re-queued %d Google News links for checking", n)
    if not db.get_state("migr_wayback_recheck"):
        # v2.2: image addresses were queued as articles; live paywall pages caused rejections.
        n1 = db.conn.execute(
            "UPDATE candidates SET status='rejected', last_error='image address' "
            "WHERE collector LIKE 'wayback%' AND lower(url) LIKE '%/alternates/%'").rowcount
        n2 = db.conn.execute(
            "UPDATE candidates SET status='pending', tries=0, last_error=NULL "
            "WHERE collector LIKE 'wayback%' AND status='rejected' "
            "AND lower(url) NOT LIKE '%/alternates/%' AND wayback_ts != ''").rowcount
        db.conn.commit()
        db.set_state("migr_wayback_recheck", True)
        log.info("marked %d image links; re-queued %d history links for archive re-check", n1, n2)
    if not db.get_state("migr_slim_db"):
        # v2.3: archaeology.org flooded the queue (every address contains 'archaeology'),
        # and very long photo-credit addresses bloated the database past GitHub's 100 MB limit.
        n1 = db.conn.execute(
            "DELETE FROM candidates WHERE status != 'matched' AND "
            "(collector IN ('wayback:archaeology.org','wayback:heritagedaily.com',"
            "'wayback:arkeonews.net','wayback:ancient-origins.net') OR length(url) > 300)").rowcount
        st = db.get_state("wayback_v3")
        if st:
            st["resume"] = None          # keyword list for these sites changed
            db.set_state("wayback_v3", st)
        db.conn.commit()
        db.set_state("migr_slim_db", True)
        log.info("removed %d unneeded links from the database", n1)
    if not db.get_state("migr_tidy_urls"):
        # v2.3: merge mirror/port addresses; re-check international sites with the stricter rule.
        from .urls import tidy
        merged = 0
        for table in ("candidates", "articles"):
            for (u,) in db.conn.execute(f"SELECT url FROM {table}").fetchall():
                t = tidy(u)
                if t != u:
                    if db.conn.execute(f"SELECT 1 FROM {table} WHERE url=?", (t,)).fetchone():
                        db.conn.execute(f"DELETE FROM {table} WHERE url=?", (u,))
                        merged += 1
                    else:
                        db.conn.execute(f"UPDATE {table} SET url=? WHERE url=?", (t, u))
                        if table == "articles":
                            from urllib.parse import urlparse
                            db.conn.execute("UPDATE articles SET domain=? WHERE url=?",
                                            (urlparse(t).netloc.lower().removeprefix("www."), t))
        intl = [r[0] for r in db.conn.execute(
            "SELECT url FROM articles WHERE source_country IN "
            "('USA','UK','Turkey','Egypt','Oman','Thailand','Malaysia','Singapore') "
            "AND language != 'Tamil'").fetchall()]
        for u in intl:
            db.conn.execute("DELETE FROM articles WHERE url=?", (u,))
            db.conn.execute("UPDATE candidates SET status='pending', tries=0 WHERE url=?", (u,))
        db.conn.commit()
        db.set_state("migr_tidy_urls", True)
        log.info("merged %d duplicate addresses; re-checking %d international articles", merged, len(intl))


def open_db(path) -> DB:
    """The repository stores the database gzip-compressed (GitHub rejects files over
    100 MB). Unpack it if the compressed copy is the newer one."""
    import gzip, shutil
    from pathlib import Path
    path = Path(path)
    gz = path.with_name(path.name + ".gz")
    if gz.exists() and (not path.exists() or gz.stat().st_mtime > path.stat().st_mtime):
        with gzip.open(gz, "rb") as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        log.info("unpacked %s", gz.name)
    return DB(path)


def pack_db(db: DB) -> None:
    """Compact and write the compressed copy that is committed to the repository."""
    import gzip, shutil, os
    from pathlib import Path
    path = Path(db.conn.execute("PRAGMA database_list").fetchone()[2])
    db.conn.commit()
    db.conn.execute("VACUUM")
    db.conn.close()
    gz = path.with_name(path.name + ".gz")
    tmp = gz.with_suffix(".gz.tmp")
    with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, gz)
    log.info("database %.1f MB, compressed copy %.1f MB",
             path.stat().st_size / 1e6, gz.stat().st_size / 1e6)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="collector")
    ap.add_argument("command", choices=["run", "backfill", "daily", "verify", "export", "status"])
    ap.add_argument("--minutes", type=float, default=60, help="time budget for this run")
    ap.add_argument("--db", default=str(DATA_DIR / "collector.sqlite"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    logging.getLogger("htmldate").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    from pathlib import Path
    cfg = load_config()
    db = open_db(args.db)
    matcher = Matcher(cfg)
    migrate(db)

    if args.command == "run":
        run_all(db, cfg, matcher, args.minutes)
    elif args.command == "backfill":
        run_backfill(db, cfg, matcher, args.minutes)
    elif args.command == "daily":
        run_daily(db, cfg, matcher, args.minutes)
    elif args.command == "verify":
        verify_until(db, cfg, matcher, time.time() + args.minutes * 60)
        save(db, cfg)
    elif args.command == "export":
        save(db, cfg)
    print(status(db, cfg))
    db.set_state("last_status", status(db, cfg))
    pack_db(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
