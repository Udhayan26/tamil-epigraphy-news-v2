"""Small helpers shared by collectors."""
from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_PATTERNS = [
    # /2010/04/14/  or /2010/04/14
    (re.compile(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)"), "ymd"),
    # /2020/oct/14/  (New Indian Express, Dinamani)
    (re.compile(r"/((?:19|20)\d{2})/([a-z]{3})/(\d{1,2})/", re.I), "ybd"),
    # /2017/04/02/... handled above; dtnext /2017/04/02/
    # hindu.com stories id: /stories/2010041456210700.htm
    (re.compile(r"/stories/((?:19|20)\d{2})(\d{2})(\d{2})\d+"), "ymd"),
    # -2020-10-14 or _20201014 in slug
    (re.compile(r"[-_/]((?:19|20)\d{2})-?(\d{2})-?(\d{2})(?=[-_./]|\d{0,8}$)"), "ymd"),
]


def date_from_url(url: str) -> str | None:
    for pat, kind in _PATTERNS:
        m = pat.search(url)
        if not m:
            continue
        try:
            y = int(m.group(1))
            mo = MONTHS[m.group(2).lower()] if kind == "ybd" else int(m.group(2))
            d = int(m.group(3))
            return date(y, mo, d).isoformat()
        except (ValueError, KeyError):
            continue
    return None


def date_from_ts(ts: str) -> str | None:
    """Wayback timestamp YYYYMMDDhhmmss -> YYYY-MM-DD (capture date, an upper bound)."""
    if ts and len(ts) >= 8 and ts[:8].isdigit():
        try:
            return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8])).isoformat()
        except ValueError:
            return None
    return None
