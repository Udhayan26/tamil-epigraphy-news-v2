"""Polite HTTP helper with retries and per-host rate limiting."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (compatible; TamilEpigraphyNewsCollector/1.0; "
      "+https://github.com/ research archive of public news)")

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "ta,en;q=0.8"})

_lock = threading.Lock()
_last_hit: dict[str, float] = {}

# Minimum seconds between requests to the same host.
HOST_DELAY = {
    "web.archive.org": 1.5,
    "api.gdeltproject.org": 5.5,
    "news.google.com": 2.0,
}
DEFAULT_DELAY = 1.0


def _wait(host: str) -> None:
    delay = HOST_DELAY.get(host, DEFAULT_DELAY)
    while True:
        with _lock:
            last = _last_hit.get(host, 0.0)
            now = time.monotonic()
            if now - last >= delay:
                _last_hit[host] = now
                return
            sleep_for = delay - (now - last)
        time.sleep(sleep_for)


def get(url: str, *, params: dict | None = None, timeout: float = 30,
        retries: int = 3, backoff: float = 5.0) -> requests.Response | None:
    """GET with retries. Returns the response (any status) or None on network failure."""
    host = urlparse(url).netloc
    last_exc = None
    last_resp = None
    for attempt in range(retries):
        _wait(host)
        try:
            r = _session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 502, 503, 504) and attempt < retries - 1:
                last_resp = r
                time.sleep(backoff * (attempt + 1) * (3 if r.status_code == 429 else 1))
                continue
            return r
        except requests.RequestException as e:  # timeouts, connection resets
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    if last_resp is not None:
        return last_resp
    if last_exc:
        raise last_exc
    return None
