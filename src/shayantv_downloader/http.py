"""Minimal HTTP helpers with retries (stdlib only)."""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


def fetch(url: str, *, user_agent: str, timeout: float = 30.0, attempts: int = 3) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # 404 and friends won't fix themselves on retry.
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
                raise
            if attempt == attempts:
                raise
            delay = 2**attempt
            log.warning("GET %s failed (%s), retrying in %ss", url, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_text(url: str, **kwargs) -> str:
    return fetch(url, **kwargs).decode("utf-8", errors="replace")
