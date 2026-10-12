"""Polite HTTP client shared by all sources.

* Sends a descriptive User-Agent (required by Wikimedia's API etiquette).
* Retries on 429 / 5xx, honouring ``Retry-After``.
* Optional on-disk cache so development runs don't hammer the sources.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "eu-elections-calendar/0.1 (+https://github.com/emanueleceglia/elections-calendar)"
)


class HttpClient:
    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: str | Path | None = None,
        cache_ttl_hours: float = 12.0,
        max_retries: int = 5,
        max_wait_seconds: float = 90.0,
        timeout: float = 60.0,
    ) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            user_agent or os.environ.get("ELECTIONS_CALENDAR_USER_AGENT") or DEFAULT_USER_AGENT
        )
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl_hours * 3600
        self.max_retries = max_retries
        self.max_wait = max_wait_seconds
        self.timeout = timeout
        self._last_request_per_host: dict[str, float] = {}

    # ------------------------------------------------------------------ cache
    def _cache_path(self, method: str, url: str, params: Any, data: Any) -> Path | None:
        if not self.cache_dir:
            return None
        key = json.dumps([method, url, params, data], sort_keys=True, default=str)
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}.bin"

    def _read_cache(self, path: Path | None) -> bytes | None:
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        return path.read_bytes()

    def _write_cache(self, path: Path | None, content: bytes) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # --------------------------------------------------------------- requests
    def _throttle(self, url: str, min_interval: float) -> None:
        host = requests.utils.urlparse(url).netloc
        last = self._last_request_per_host.get(host)
        if last is not None:
            wait = min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_per_host[host] = time.monotonic()

    def request_bytes(
        self,
        url: str,
        params: dict | None = None,
        method: str = "GET",
        data: dict | None = None,
        headers: dict | None = None,
        min_interval: float = 1.0,
        timeout: float | None = None,
    ) -> bytes:
        cache_path = self._cache_path(method, url, params, data)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        attempt = 0
        while True:
            attempt += 1
            self._throttle(url, min_interval)
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, headers=headers,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise
                wait = min(2**attempt, self.max_wait)
                log.warning("%s failed (%s); retrying in %.0fs", url, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else 2**attempt * 2
                wait = min(wait + 1, self.max_wait)
                log.warning("%s -> HTTP %s; retrying in %.0fs", url, resp.status_code, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            self._write_cache(cache_path, resp.content)
            return resp.content

    def get_json(self, url: str, params: dict | None = None, **kwargs: Any) -> Any:
        return json.loads(self.request_bytes(url, params=params, **kwargs))

    def get_text(self, url: str, params: dict | None = None, **kwargs: Any) -> str:
        return self.request_bytes(url, params=params, **kwargs).decode("utf-8", errors="replace")
