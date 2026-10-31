"""Polite HTTP access to OpenAlex, Crossref and the DOI registry, with an on-disk cache.

Every response is cached by URL so a rebuild costs no extra requests and is reproducible.
Requests are spaced per host (OpenAlex allows 10 per second; we use far less) and retried with
backoff on 429 and 5xx. No email address or other personal data is sent.
"""

import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

USER_AGENT = "BAYAN-reliability/1.0 (+https://github.com/mohsalah/BAYAN)"
MIN_INTERVAL = {"api.openalex.org": 0.15, "api.crossref.org": 0.2, "doi.org": 0.2,
                "www.ebi.ac.uk": 0.2, "openlibrary.org": 1.0, "hdl.handle.net": 0.5,
                "export.arxiv.org": 3.0}  # arXiv asks for one request every 3 seconds


class FetchError(Exception):
    pass


class Client:
    def __init__(self, cache_dir: Path, openalex_key: str | None = None, timeout: float = 30.0,
                 offline: bool = False, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.openalex_key = openalex_key
        self.offline = offline
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True, transport=transport)
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.requests = self.cache_hits = 0

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _wait(self, host: str) -> None:
        gap = MIN_INTERVAL.get(host, 0.5)
        with self._lock:
            now = time.monotonic()
            delay = self._last.get(host, 0) + gap - now
            self._last[host] = max(now, self._last.get(host, 0) + gap)
        if delay > 0:
            self._sleep(delay)

    def get_text(self, url: str, params: dict | None = None) -> str | None:
        """The body as text (e.g. arXiv's Atom XML), or None for a 404; cached like JSON."""
        return self.get_json(url, params, as_text=True)

    def get_json(self, url: str, params: dict | None = None, as_text: bool = False):
        """The JSON body, or None for a 404. Raises FetchError after retries."""
        full = f"{url}?{urlencode(params)}" if params else url
        path = self._path(full)
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())["body"]
        if self.offline:
            raise FetchError(f"not cached (offline): {full}")
        send = dict(params or {})
        if self.openalex_key and "api.openalex.org" in url:
            send["api_key"] = self.openalex_key  # sent, but never part of the cache key
        host = httpx.URL(url).host
        for attempt in range(5):
            self._wait(host)
            try:
                r = self._http.get(url, params=send)
            except httpx.HTTPError as exc:
                err = type(exc).__name__
            else:
                self.requests += 1
                if r.status_code == 404:
                    body = None
                    break
                if r.status_code == 200:
                    body = r.text if as_text else r.json()
                    break
                err = f"HTTP {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise FetchError(f"{err} for {full}")
            self._sleep(min(60, 2 ** attempt * 2))
        else:
            raise FetchError(f"{err} for {full} after retries")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": full, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                          time.gmtime()),
                                   "body": body}))
        tmp.replace(path)
        return body

    def fetched_at(self, url: str, params: dict | None = None) -> str:
        full = f"{url}?{urlencode(params)}" if params else url
        path = self._path(full)
        return json.loads(path.read_text())["fetched"] if path.exists() else ""
