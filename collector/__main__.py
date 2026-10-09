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
from .sources import date_archive, gdelt, gnews, wayback
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
           + f"new articles today: {res['new_today']}   |   last 7 days: {res['new_week']}\n"
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


def search_recent(db: DB, cfg) -> None:
    """Latest news from Google News (Tamil + English) and GDELT."""
    try:
        n1 = gnews.recent(db, cfg, days=7)
    except Exception as e:
        log.warning("Google News search failed: %s", e)
        n1 = 0
    try:
        n2 = gdelt.recent(db, cfg, days=3)
    except Exception as e:
        log.warning("GDELT search failed: %s", e)
        n2 = 0
    log.info("recent news: %d new links from Google News, %d from GDELT", n1, n2)


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
    search_recent(db, cfg)
    # Check the fresh news within the first ~15 minutes so it is never crowded out.
    verify_until(db, cfg, matcher, start + min(15, minutes * 0.3) * 60)
    remaining = minutes - (time.time() - start) / 60
    if remaining > 5 and not db.get_state("backfill_done", False):
        run_backfill(db, cfg, matcher, remaining)
    else:
        verify_until(db, cfg, matcher, start + minutes * 60)
        save(db, cfg)


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
    db = DB(Path(args.db))
    matcher = Matcher(cfg)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
