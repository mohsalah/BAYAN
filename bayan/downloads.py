"""Legal PDF download: SSRF-safe polite fetching, PDF validation, identity check, storage.

A PDF may only come from (in order) a file the user uploaded, the open-access URL the library
listed, or an Unpaywall lookup by DOI. Nothing else is tried, and nothing is saved as a PDF
until it has passed validation (see AGENTS.md).
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from pypdf import PdfReader

from .config import Settings
from .db import connect

log = logging.getLogger("bayan.downloads")

MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 5
IDENTITY_PAGES = 3
TITLE_MATCH = 0.9  # normalised similarity needed for downloaded_verified
TITLE_MISMATCH = 0.5  # below this, with enough text, the file is a suspected other paper
MIN_TEXT_FOR_MISMATCH = 200
UNPAYWALL = "https://api.unpaywall.org/v2/"

VERIFIED = "downloaded_verified"
UNVERIFIED = "downloaded_unverified_identity"
MISMATCH = "identity_mismatch"
NEEDS_ACCESS = "needs_access"
PENDING = "pending"
DOWNLOADED = (VERIFIED, UNVERIFIED)

REASONS = {
    "no_open_link": "No open-access link is listed for this paper.",
    "html_returned": "The link returned a web page (HTML), not a PDF.",
    "http_error": "The server answered with an HTTP error.",
    "too_large": "The file is larger than the allowed size.",
    "not_pdf": "The file is not a valid PDF.",
    "unsafe_url": "The link is not a public http(s) address and was not requested.",
    "network_error": "The server could not be reached.",
    "internal_error": "The download failed unexpectedly.",
}


# --------------------------------------------------------------------------- URL safety

class UnsafeURL(ValueError):
    pass


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"host does not resolve: {host}") from exc
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_public_url(url: str, resolver: Resolver = system_resolver) -> str:
    """Return the host of a public http(s) URL, or raise UnsafeURL. Checked on every hop."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("malformed URL") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("URL has no host")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolver(host, port or (443 if parts.scheme == "https" else 80))
    if not addresses:
        raise UnsafeURL(f"host does not resolve: {host}")
    for address in addresses:
        if not _is_public(address):
            raise UnsafeURL("address is private, loopback or link-local")
    return host.lower()


# --------------------------------------------------------------------------- fetching

class BodyRejected(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


@dataclass
class FetchResult:
    ok: bool
    code: str = ""
    detail: str = ""
    status: int | None = None
    final_url: str = ""
    value: object = None


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after", "").strip()
    return float(raw) if raw.isdigit() else None


class Fetcher:
    """One request per host at a time, a minimum delay per host, Retry-After, bounded retries.

    Redirects are followed by hand so every hop is checked for SSRF.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 resolver: Resolver = system_resolver, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, host_delay: float = 1.0,
                 max_retries: int = 2, max_redirects: int = MAX_REDIRECTS, timeout: float = 30.0,
                 max_wait: float = 60.0, contact_email: str | None = None):
        agent = "BAYAN/0.1 (research assistant"
        agent += f"; mailto:{contact_email})" if contact_email else ")"
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False,
                                    headers={"User-Agent": agent})
        self._resolver, self._sleep, self._clock = resolver, sleep, clock
        self._host_delay, self._max_retries = host_delay, max_retries
        self._max_redirects, self._max_wait = max_redirects, max_wait
        self._locks: dict[str, threading.Lock] = {}
        self._next_ok: dict[str, float] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def _push_back(self, host: str, seconds: float) -> None:
        with self._guard:
            self._next_ok[host] = max(self._next_ok.get(host, 0.0),
                                      self._clock() + min(seconds, self._max_wait))

    def _once(self, url: str, host: str, handle: Callable[[httpx.Response], object]) -> tuple:
        with self._lock_for(host):
            wait = self._next_ok.get(host, 0.0) - self._clock()
            if wait > 0:
                self._sleep(wait)
            delay = self._host_delay
            try:
                with self._client.stream("GET", url) as resp:
                    status = resp.status_code
                    if status in (301, 302, 303, 307, 308):
                        return "redirect", status, resp.headers.get("location", "")
                    if status == 429 or status >= 500:
                        return "retry", status, _retry_after(resp)
                    if status >= 400:
                        return "error", status, None
                    return "ok", status, handle(resp)
            finally:
                self._push_back(host, delay)

    def fetch(self, url: str, handle: Callable[[httpx.Response], object]) -> FetchResult:
        hops = 0
        while True:
            try:
                host = check_public_url(url, self._resolver)
            except UnsafeURL as exc:
                return FetchResult(False, "unsafe_url", str(exc), final_url=_plain(url))
            outcome = None
            for attempt in range(self._max_retries + 1):
                try:
                    outcome = self._once(url, host, handle)
                except BodyRejected as exc:
                    return FetchResult(False, exc.code, exc.detail, final_url=_plain(url))
                except httpx.HTTPError as exc:
                    outcome = ("network", None, type(exc).__name__)
                if outcome[0] not in ("retry", "network"):
                    break
                retry_after = outcome[2] if outcome[0] == "retry" else None
                self._push_back(host, retry_after if retry_after is not None
                                else self._host_delay * 2 ** attempt)
            kind, status, value = outcome
            if kind == "redirect":
                hops += 1
                if hops > self._max_redirects:
                    return FetchResult(False, "http_error", "Too many redirects.", status,
                                       _plain(url))
                if not value:
                    return FetchResult(False, "http_error", "Redirect without a location.",
                                       status, _plain(url))
                url = urljoin(url, value)
                continue
            if kind == "ok":
                return FetchResult(True, status=status, final_url=_plain(url), value=value)
            if kind == "network":
                return FetchResult(False, "network_error", f"Request failed ({value}).",
                                   final_url=_plain(url))
            return FetchResult(False, "http_error", f"HTTP {status}.", status, _plain(url))

    def close(self) -> None:
        self._client.close()


def _plain(url: str) -> str:
    """The URL without query string, so e-mail addresses and tokens are never stored."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()


def _looks_html(head: bytes, content_type: str) -> bool:
    low = head.lstrip().lower()
    return "html" in content_type or low.startswith((b"<!doctype", b"<html", b"<head", b"<body"))


def pdf_sink(part: Path, max_bytes: int = MAX_PDF_BYTES) -> Callable[[httpx.Response], dict]:
    """Stream a response to `part`, refusing early when it is oversize, HTML or not a PDF."""

    def handle(resp: httpx.Response) -> dict:
        ctype = resp.headers.get("content-type", "").lower()
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise BodyRejected("too_large", "The file is larger than the allowed size.")
        size, head = 0, b""
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "wb") as out:
            for chunk in resp.iter_bytes(65536):
                if len(head) < 5:
                    head += chunk[: 5 - len(head)]
                    if len(head) >= 5 and head != b"%PDF-":
                        raise _not_pdf(chunk, ctype)
                size += len(chunk)
                if size > max_bytes:
                    raise BodyRejected("too_large", "The file is larger than the allowed size.")
                out.write(chunk)
        return {"size": size, "content_type": ctype}

    return handle


def _not_pdf(chunk: bytes, ctype: str) -> BodyRejected:
    if _looks_html(chunk, ctype):
        return BodyRejected("html_returned", REASONS["html_returned"])
    return BodyRejected("not_pdf", REASONS["not_pdf"])


def json_sink(resp: httpx.Response) -> dict:
    body = b""
    for chunk in resp.iter_bytes(65536):
        body += chunk
        if len(body) > 2_000_000:
            raise BodyRejected("not_pdf", "Response too large.")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise BodyRejected("http_error", "Response was not JSON.") from exc
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- validation

@dataclass
class Validation:
    ok: bool
    code: str = ""
    detail: str = ""
    size: int = 0
    sha256: str = ""
    text: str = ""


def validate_pdf(path: Path, max_bytes: int = MAX_PDF_BYTES) -> Validation:
    """Magic bytes, non-empty, size cap and a real parse. Also returns first-pages text."""
    size = path.stat().st_size
    if size == 0:
        return Validation(False, "not_pdf", "The file is empty.")
    if size > max_bytes:
        return Validation(False, "too_large", REASONS["too_large"])
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        head = f.read(65536)
        digest.update(head)
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    if not head.startswith(b"%PDF-"):
        code = "html_returned" if _looks_html(head[:512], "") else "not_pdf"
        return Validation(False, code, REASONS[code])
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            return Validation(False, "not_pdf", "The PDF is password-protected.")
        pages = len(reader.pages)
        if pages < 1:
            return Validation(False, "not_pdf", "The PDF has no pages.")
        text = ""
        for i in range(min(pages, IDENTITY_PAGES)):
            try:
                text += (reader.pages[i].extract_text() or "") + "\n"
            except Exception:  # unreadable page text only weakens the identity check
                continue
    except Exception:
        return Validation(False, "not_pdf", "The file could not be read as a PDF.")
    return Validation(True, size=size, sha256=digest.hexdigest(), text=text)


# --------------------------------------------------------------------------- identity

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_similarity(title: str, text: str) -> float:
    """Best normalised similarity between the title and any stretch of the text (0..1)."""
    t, body = _norm(title), _norm(text)[:8000]
    if len(t) < 8 or not body:
        return 0.0
    if t in body:
        return 1.0
    width, best = int(len(t) * 1.15) + 1, 0.0
    step = max(1, len(t) // 6)
    for start in range(0, max(1, len(body) - len(t) // 2), step):
        matcher = SequenceMatcher(None, t, body[start:start + width], autojunk=False)
        # Share of the title's characters found in order, ignoring tiny scattered matches.
        covered = sum(b.size for b in matcher.get_matching_blocks() if b.size >= 4)
        best = max(best, covered / len(t))
        if best >= 1.0:
            break
    return best


def doi_in_text(doi: str, text: str) -> bool:
    doi = doi.strip().lower()
    if not doi:
        return False
    # Whitespace may appear inside a printed DOI (line wraps) but is not removed between words.
    spaced = r"\s*".join(re.escape(c) for c in re.sub(r"\s+", "", doi))
    return re.search(r"(?<![a-z0-9])" + spaced + r"(?![a-z0-9])", text.lower()) is not None


def check_identity(title: str, doi: str, text: str) -> tuple[str, float, str]:
    """-> (status, score, basis). A DOI printed in the text or a close title verifies."""
    score = title_similarity(title, text)
    if doi_in_text(doi, text):
        return VERIFIED, score, "doi"
    if score >= TITLE_MATCH:
        return VERIFIED, score, "title"
    if len(_norm(text)) < MIN_TEXT_FOR_MISMATCH:
        return UNVERIFIED, score, "no_text"
    if score < TITLE_MISMATCH and len(_norm(title)) >= 8:
        return MISMATCH, score, "title_differs"
    return UNVERIFIED, score, "title_partial"


# --------------------------------------------------------------------------- storage

def safe_name(value: str) -> str:
    """Injective, filesystem-safe form of an id (no separators or dots)."""
    name = re.sub(r"[^A-Za-z0-9_-]", lambda m: "".join(f"%{b:02X}" for b in m.group().encode()),
                  value)
    if not name or len(name) > 120:
        return "h-" + hashlib.sha256(value.encode()).hexdigest()[:40]
    return name


def pdf_path(root: Path, user_id: int, source: str, paper_id: str) -> Path:
    return root / str(user_id) / safe_name(source) / (safe_name(paper_id) + ".pdf")


def quarantine_path(root: Path, user_id: int, source: str, paper_id: str) -> Path:
    return (root / str(user_id) / "_identity_mismatch" / safe_name(source)
            / (safe_name(paper_id) + ".pdf"))


def new_part(root: Path, user_id: int, source: str, paper_id: str) -> Path:
    final = pdf_path(root, user_id, source, paper_id)
    return final.with_name(f"{final.name}.{uuid.uuid4().hex[:8]}.part")


@dataclass
class Verdict:
    status: str
    reason: str = ""
    detail: str = ""
    path: Path | None = None
    size: int = 0
    sha256: str = ""
    score: float | None = None
    basis: str = ""


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def finalize_pdf(part: Path, root: Path, user_id: int, source: str, paper_id: str,
                 title: str, doi: str, max_bytes: int = MAX_PDF_BYTES) -> Verdict:
    """Validate `part`, check identity, then move it atomically into place (or delete it)."""
    result = validate_pdf(part, max_bytes)
    if not result.ok:
        _unlink(part)
        return Verdict(NEEDS_ACCESS, result.code, result.detail)
    status, score, basis = check_identity(title, doi, result.text)
    final = pdf_path(root, user_id, source, paper_id)
    dest = quarantine_path(root, user_id, source, paper_id) if status == MISMATCH else final
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(part, "rb") as f:
        os.fsync(f.fileno())
    os.replace(part, dest)
    if status == MISMATCH:
        _unlink(final)  # a mismatching file must never remain offered as this paper
    detail = {"doi": "The paper's DOI appears in the file.",
              "title": "The title matches the first pages.",
              "no_text": "No readable text in the first pages, so identity could not be checked.",
              "title_partial": "The title only partly matches the first pages.",
              "title_differs": "The first pages do not match this paper's title."}[basis]
    return Verdict(status, detail=detail, path=dest, size=result.size, sha256=result.sha256,
                   score=round(score, 3), basis=basis)


# --------------------------------------------------------------------------- records

def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


FIELDS = ("status", "reason", "detail", "origin", "path", "size", "sha256", "source_url",
          "final_url", "http_status", "retrieved_at", "license", "version", "identity_score",
          "identity_basis")
PUBLIC_FIELDS = tuple(f for f in FIELDS if f != "path")


def save_record(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str,
                **values: object) -> None:
    row = {f: values.get(f) for f in FIELDS}
    cols = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    updates = ", ".join(f"{f}=excluded.{f}" for f in FIELDS)
    conn.execute(
        f"INSERT INTO downloads(user_id, source, paper_id, {cols}) VALUES(?,?,?,{marks})"
        f" ON CONFLICT(user_id, source, paper_id) DO UPDATE SET {updates},"
        " updated_at=CURRENT_TIMESTAMP",
        (user_id, source, paper_id, *row.values()))
    conn.commit()


def record_verdict(conn: sqlite3.Connection, user_id: int, source: str, paper_id: str,
                   verdict: Verdict, **meta: object) -> None:
    save_record(
        conn, user_id, source, paper_id, status=verdict.status, reason=verdict.reason or None,
        detail=verdict.detail or None, path=str(verdict.path) if verdict.path else None,
        size=verdict.size or None, sha256=verdict.sha256 or None,
        identity_score=verdict.score, identity_basis=verdict.basis or None, **meta)


def public_record(row: sqlite3.Row) -> dict:
    out = {"source": row["source"], "id": row["paper_id"]}
    out.update({f: row[f] for f in PUBLIC_FIELDS})
    out["has_file"] = row["status"] in DOWNLOADED
    out["reason_text"] = REASONS.get(row["reason"] or "", "")
    return out


# --------------------------------------------------------------------------- the job

@dataclass
class Candidate:
    url: str
    origin: str  # "oa_url" or "unpaywall"
    license: str = ""
    version: str = ""


def unpaywall_candidates(fetcher: Fetcher, doi: str, email: str) -> tuple[list[Candidate], str]:
    """PDF links Unpaywall lists for a DOI. Returns (candidates, note when none/failed)."""
    url = UNPAYWALL + quote(doi, safe="/") + "?" + urlencode({"email": email})
    res = fetcher.fetch(url, json_sink)
    if not res.ok:
        if res.status == 404:
            return [], "Unpaywall has no record for this DOI."
        return [], f"Unpaywall lookup failed ({res.detail})"
    data = res.value if isinstance(res.value, dict) else {}
    locations = [data.get("best_oa_location")] + list(data.get("oa_locations") or [])
    out: list[Candidate] = []
    for loc in locations:
        if isinstance(loc, dict) and loc.get("url_for_pdf") \
                and all(loc["url_for_pdf"] != c.url for c in out):
            out.append(Candidate(loc["url_for_pdf"], "unpaywall", str(loc.get("license") or ""),
                                 str(loc.get("version") or "")))
    if not out:
        return [], "Unpaywall lists no open PDF for this DOI."
    return out[:3], ""


def process_paper(conn: sqlite3.Connection, fetcher: Fetcher, settings: Settings,
                  user_id: int, source: str, paper_id: str) -> None:
    paper = conn.execute("SELECT * FROM papers WHERE source = ? AND id = ?",
                         (source, paper_id)).fetchone()
    if paper is None:
        save_record(conn, user_id, source, paper_id, status=NEEDS_ACCESS, reason="no_open_link",
                    detail="The paper record is no longer available.")
        return
    current = conn.execute(
        "SELECT status FROM downloads WHERE user_id = ? AND source = ? AND paper_id = ?",
        (user_id, source, paper_id)).fetchone()
    if current and current["status"] in DOWNLOADED:
        return  # an uploaded or earlier validated copy wins; nothing to fetch
    tried: set[str] = set()
    failures: list[tuple[str, str]] = []  # (reason code, sentence)
    last: FetchResult | None = None
    last_url = ""

    def attempt(cand: Candidate) -> bool:
        nonlocal last, last_url
        tried.add(cand.url)
        part = new_part(settings.pdf_root, user_id, source, paper_id)
        last_url = _plain(cand.url)
        res = fetcher.fetch(cand.url, pdf_sink(part, settings.pdf_max_bytes))
        last = res
        if not res.ok:
            _unlink(part)
            failures.append((res.code, f"{cand.origin}: {res.detail}"))
            return False
        verdict = finalize_pdf(part, settings.pdf_root, user_id, source, paper_id,
                               paper["title"] or "", paper["doi"] or "", settings.pdf_max_bytes)
        if verdict.status == NEEDS_ACCESS:
            failures.append((verdict.reason, f"{cand.origin}: {verdict.detail}"))
            return False
        record_verdict(conn, user_id, source, paper_id, verdict, origin=cand.origin,
                       source_url=last_url, final_url=res.final_url, http_status=res.status,
                       retrieved_at=now_iso(), license=cand.license or None,
                       version=cand.version or None)
        return True

    if paper["oa_url"] and attempt(Candidate(paper["oa_url"], "oa_url")):
        return
    if paper["doi"]:
        if settings.contact_email:
            cands, note = unpaywall_candidates(fetcher, paper["doi"], settings.contact_email)
            if note:
                failures.append(("", note))
            for cand in cands:
                if cand.url not in tried and attempt(cand):
                    return
        else:
            failures.append(("", "Unpaywall was not queried: CONTACT_EMAIL is not configured."))
    reason = next((code for code, _ in failures if code), "no_open_link")
    detail = " ".join(text for _, text in failures) or REASONS["no_open_link"]
    save_record(conn, user_id, source, paper_id, status=NEEDS_ACCESS, reason=reason,
                detail=detail, source_url=last_url or None,
                final_url=(last.final_url or None) if last else None,
                http_status=last.status if last else None, retrieved_at=now_iso())


def run_download_job(db_path: Path, job_id: int, fetcher: Fetcher, settings: Settings) -> None:
    """Background job body: works through the queued papers one by one."""
    conn = connect(db_path)
    try:
        job = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        payload = json.loads(job["payload"])
        set_job(conn, job_id, "running")
        for source, paper_id in payload["papers"]:
            try:
                process_paper(conn, fetcher, settings, payload["user_id"], source, paper_id)
            except Exception as exc:
                log.warning("download failed for %s/%s: %s", source, paper_id, type(exc).__name__)
                save_record(conn, payload["user_id"], source, paper_id, status=NEEDS_ACCESS,
                            reason="internal_error", detail=REASONS["internal_error"])
        set_job(conn, job_id, "done")
    except Exception as exc:
        log.warning("download job %s failed: %s", job_id, type(exc).__name__)
        set_job(conn, job_id, "failed", type(exc).__name__)
    finally:
        conn.close()


def set_job(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE jobs SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP"
                 " WHERE id = ?", (status, error, job_id))
    conn.commit()
