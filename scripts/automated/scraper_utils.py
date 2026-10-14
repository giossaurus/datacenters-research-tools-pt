# Copyright (c) 2025-2026 Giovanni Della Dea
"""
scraper_utils.py
Shared utilities for polite HTTP scraping:
  - Random User-Agent rotation (fake-useragent with static fallback)
  - Random inter-request delays
  - Exponential backoff on HTTP 429
  - Optional proxy rotation via itertools.cycle
  - handle_interruption() logs a soft block so the city can be collected manually

Usage:
    from scraper_utils import build_session, fetch_with_backoff, random_delay, handle_interruption
"""

from __future__ import annotations

import itertools
import logging
import random
import time
from typing import Iterator

import requests

logger = logging.getLogger(__name__)

_FALLBACK_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

try:
    from fake_useragent import UserAgent as _UA
    _ua_generator = _UA()
    def _random_ua() -> str:
        return _ua_generator.random
    logger.debug("fake-useragent loaded successfully.")
except Exception:
    logger.warning("fake-useragent unavailable: using built-in fallback UA list.")
    def _random_ua() -> str:
        return random.choice(_FALLBACK_UAS)


def random_headers() -> dict:
    return {
        "User-Agent": _random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Sem "br": sem brotli instalado o corpo fica ilegível ao parser.
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        # Pistas de navegador real: reduz respostas “vazias” / 429 em alguns edges.
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


def random_delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    delay = random.uniform(min_s, max_s)
    logger.debug("Delay: %.2fs", delay)
    time.sleep(delay)


def make_proxy_cycle(proxy_list: list[str]) -> Iterator[dict] | None:
    if not proxy_list:
        return None
    dicts = [{"http": p, "https": p} for p in proxy_list]
    return itertools.cycle(dicts)


_BLOCK_KEYWORDS = (
    "access denied",
    "just a moment",
    "cf-challenge",
    "enable javascript",
    "403 forbidden",
    "bot detection",
    "vercel security checkpoint",
    "security checkpoint",
    "we're verifying your browser",
    "verifying your browser",
)


def is_blocked(response: requests.Response) -> bool:
    if not response.text.strip():
        return True
    lower = response.text[:4096].lower()
    return any(kw in lower for kw in _BLOCK_KEYWORDS)


def handle_interruption(url: str, site_key: str | None = None) -> bool:
    """Log a soft block and stop; blocked pages are collected by hand (scripts/manual/)."""
    logger.warning("Possível bloqueio em %s: coletar esta página manualmente.", url)
    return False


def fetch_with_backoff(
    session: requests.Session,
    url: str,
    proxy_cycle: Iterator[dict] | None = None,
    max_retries: int = 4,
    base_wait: float = 5.0,
    extra_headers: dict | None = None,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        headers = {**random_headers(), **(extra_headers or {})}
        proxies = next(proxy_cycle) if proxy_cycle is not None else None
        try:
            response = session.get(url, headers=headers, proxies=proxies, timeout=30)
            if response.status_code == 429:
                wait = base_wait * (2**attempt) + random.uniform(0, 3)
                logger.warning(
                    "429 Too Many Requests: backing off %.1fs (attempt %d/%d): %s",
                    wait, attempt + 1, max_retries, url,
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except requests.exceptions.ProxyError as exc:
            logger.warning(
                "Proxy error on attempt %d/%d (%s): rotating proxy: %s",
                attempt + 1, max_retries, proxies, exc,
            )
            last_exc = exc
            continue
    raise requests.HTTPError(
        f"Gave up after {max_retries} retries for: {url}"
    ) from last_exc


def build_session() -> requests.Session:
    session = requests.Session()
    # Do not inherit proxy settings from environment variables by default.
    # We only use proxies explicitly provided in config.yaml (proxy_cycle).
    session.trust_env = False
    session.headers.update(random_headers())
    return session
