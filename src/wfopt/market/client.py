"""warframe.market HTTP client: descriptive UA, token-bucket rate limit, retry/backoff, disk cache."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from wfopt.config import Config

API = "https://api.warframe.market"
RETRY_STATUS = {429, 500, 502, 503, 504, 509, 520, 521, 522, 524}


class WfmError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimiter:
    """Token bucket with capacity 1: guarantees at most `rps` request starts per second."""

    def __init__(self, rps: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    def acquire(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


@dataclass
class CacheEntry:
    fetched_at: float
    body: Any


class DiskCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode()).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)[-80:]
        return self.root / f"{safe}.{h}.json"

    def get(self, key: str) -> CacheEntry | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return CacheEntry(d["fetched_at"], d["body"])
        except (OSError, ValueError, KeyError):
            return None

    def put(self, key: str, body: Any, fetched_at: float | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "fetched_at": fetched_at or time.time(), "body": body}), encoding="utf-8")
        tmp.replace(p)


class WfmClient:
    def __init__(
        self,
        cfg: Config,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
    ):
        self.cfg = cfg
        w = cfg.wfm
        self.headers = {
            "User-Agent": w.user_agent,
            "Platform": w.platform,
            "Language": w.language,
            "Crossplay": "true" if w.crossplay else "false",
            "Accept": "application/json",
        }
        self.http = http or httpx.Client(timeout=30.0)
        self.limiter = limiter or RateLimiter(w.rps)
        self.cache = DiskCache(cfg.cache_dir / "http")
        self._sleep = sleep
        self.max_retries = max_retries
        self.network_calls = 0
        self.token: str | None = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "WfmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level -----------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        headers = dict(self.headers)
        if auth:
            if not self.token:
                raise WfmError("Not authenticated")
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            self.network_calls += 1
            try:
                resp = self.http.request(method, url, params=params, json=json_body, headers=headers)
            except httpx.TransportError as e:
                last_exc = e
                resp = None
            if resp is not None and resp.status_code not in RETRY_STATUS:
                return resp
            if attempt == self.max_retries:
                break
            wait = delay
            if resp is not None:
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, float(ra))
            self._sleep(wait + random.uniform(0, 0.25 * wait))
            delay = min(delay * 2, 60)
        if resp is not None:
            raise WfmError(f"{method} {url} failed with {resp.status_code} after retries", resp.status_code, resp.text)
        raise WfmError(f"{method} {url} failed: {last_exc}")

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        ttl_minutes: float = 0,
        refresh: bool = False,
        allow_404: bool = False,
    ) -> Any:
        """GET with disk cache. `ttl_minutes <= 0` disables caching."""
        key = self._key(path, params)
        if ttl_minutes > 0 and not refresh:
            hit = self.cache.get(key)
            if hit and time.time() - hit.fetched_at < ttl_minutes * 60:
                return hit.body
        resp = self.request("GET", path, params=params)
        if resp.status_code == 404 and allow_404:
            body: Any = None
        elif resp.status_code >= 400:
            raise WfmError(f"GET {path} -> {resp.status_code}", resp.status_code, resp.text[:500])
        else:
            body = resp.json()
        if ttl_minutes > 0:
            self.cache.put(key, body)
        return body

    def _key(self, path: str, params: dict[str, Any] | None) -> str:
        q = "?" + "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        return f"{self.cfg.wfm.platform}{'+x' if self.cfg.wfm.crossplay else ''}:{path}{q}"

    def cached_only(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Return a cached body regardless of age, without touching the network."""
        hit = self.cache.get(self._key(path, params))
        return hit.body if hit else None


def v2_data(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, dict) and body.get("error"):
        raise WfmError(f"WFM error: {body['error']}")
    return body.get("data") if isinstance(body, dict) else body
