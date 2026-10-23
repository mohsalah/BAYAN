"""Membership access (M7): encrypted per-user publisher keys and the institutional proxy link.

Keys are encrypted with Fernet; the Fernet key is derived from APP_SECRET_KEY with HKDF and a
fixed context string. Without a real secret nothing is saved (no plaintext fallback). The full
key leaves this module only through `get_user_key`, for a future connector; no endpoint, log line,
error message, run summary or export ever carries it.

The proxy link is only a link the user opens in their own browser while logged in to their
institution. BAYAN never logs in, never fetches through the proxy and stores no library password.
"""

import base64
import ipaddress
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Settings, load_settings
from .db import connect
from .downloads import _is_public

KDF_CONTEXT = b"bayan.membership.credentials.v1"
MIN_SECRET_LENGTH = 16
PLACEHOLDER_SECRETS = {"change-me", "changeme", "change_me", "your-key-here", "secret",
                       "your-secret-here", "replace-me", "placeholder"}
MIN_KEY_LENGTH, MAX_KEY_LENGTH = 8, 512
MAX_TEMPLATE_LENGTH = 500
MAX_TARGET_LENGTH = 2048
PLACEHOLDERS = ("{url}", "{doi}")
EXAMPLE_DOI = "10.1000/xyz123"
_DOI = re.compile(r"^10\.\d{1,9}/\S+$")
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")
LAST_CHARS = 4
MIN_LENGTH_FOR_LAST_CHARS = 12  # shorter keys would reveal too much of themselves


class SecretNotConfigured(RuntimeError):
    pass


class InvalidInput(ValueError):
    pass


def secret_problem(secret: str | None) -> str:
    """Why the secret cannot be used, or an empty string when it is acceptable."""
    value = (secret or "").strip()
    if not value:
        return "APP_SECRET_KEY is not set"
    if value.lower() in PLACEHOLDER_SECRETS or value.lower().startswith(("change-me", "your-")):
        return "APP_SECRET_KEY is still a placeholder"
    if len(value) < MIN_SECRET_LENGTH:
        return f"APP_SECRET_KEY is shorter than {MIN_SECRET_LENGTH} characters"
    return ""


def fernet_for(secret: str | None) -> Fernet:
    problem = secret_problem(secret)
    if problem:
        raise SecretNotConfigured(
            f"Keys cannot be stored: {problem}. Set a random APP_SECRET_KEY on the server.")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=KDF_CONTEXT).derive(
        (secret or "").strip().encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_key(raw: object) -> str:
    """The key as sent, minus surrounding whitespace. Errors never repeat the value."""
    key = raw.strip() if isinstance(raw, str) else ""
    if not (MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH) or _BAD_CHARS.search(key):
        raise InvalidInput(
            f"The key must be {MIN_KEY_LENGTH} to {MAX_KEY_LENGTH} characters with no spaces.")
    return key


def save_key(conn: sqlite3.Connection, secret: str | None, user_id: int, publisher: str,
             key: str) -> str:
    token = fernet_for(secret).encrypt(key.encode()).decode()
    saved_at = _now()
    conn.execute(
        "INSERT INTO membership_keys(user_id, publisher, ciphertext, saved_at) VALUES(?,?,?,?)"
        " ON CONFLICT(user_id, publisher) DO UPDATE SET ciphertext = excluded.ciphertext,"
        " saved_at = excluded.saved_at", (user_id, publisher, token, saved_at))
    conn.commit()
    return saved_at


def delete_key(conn: sqlite3.Connection, user_id: int, publisher: str) -> bool:
    cur = conn.execute("DELETE FROM membership_keys WHERE user_id = ? AND publisher = ?",
                       (user_id, publisher))
    conn.commit()
    return cur.rowcount > 0


def _decrypt(secret: str | None, ciphertext: str) -> str | None:
    """The plaintext, or None when it cannot be read (rotated secret, damaged data, no secret)."""
    try:
        return fernet_for(secret).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, SecretNotConfigured, ValueError):
        return None


def key_status(conn: sqlite3.Connection, secret: str | None, user_id: int,
               publisher: str) -> dict:
    """Status for the UI. Shows at most the last four characters of a long enough key."""
    row = conn.execute(
        "SELECT ciphertext, saved_at FROM membership_keys WHERE user_id = ? AND publisher = ?",
        (user_id, publisher)).fetchone()
    if row is None:
        return {"saved": False, "needs_reentry": False, "last4": None, "saved_at": None}
    key = _decrypt(secret, row["ciphertext"])
    if key is None:
        return {"saved": True, "needs_reentry": True, "last4": None, "saved_at": row["saved_at"]}
    last = key[-LAST_CHARS:] if len(key) >= MIN_LENGTH_FOR_LAST_CHARS else None
    return {"saved": True, "needs_reentry": False, "last4": last, "saved_at": row["saved_at"]}


def get_user_key(user_id: int, publisher: str, settings: Settings | None = None) -> str | None:
    """For a future connector: the user's decrypted key, or None if none is saved or it cannot
    be read (then the user must re-enter it). Never log or return the value to a client."""
    settings = settings or load_settings()
    with closing(connect(settings.database_path)) as conn:
        row = conn.execute(
            "SELECT ciphertext FROM membership_keys WHERE user_id = ? AND publisher = ?",
            (user_id, publisher)).fetchone()
    return _decrypt(settings.app_secret_key, row["ciphertext"]) if row else None


# --------------------------------------------------------------------------- proxy template

def validate_template(raw: object) -> str:
    template = raw.strip() if isinstance(raw, str) else ""
    if not template:
        raise InvalidInput("Enter a proxy link template.")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise InvalidInput(f"The template is longer than {MAX_TEMPLATE_LENGTH} characters.")
    if _BAD_CHARS.search(template):
        raise InvalidInput("The template must not contain spaces or control characters.")
    if not any(p in template for p in PLACEHOLDERS):
        raise InvalidInput("The template must contain {url} (or {doi}) where the paper goes.")
    try:
        parts = urlsplit(template)
        parts.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError as exc:
        raise InvalidInput("The template is not a valid link.") from exc
    if parts.scheme != "https":
        raise InvalidInput("The template must start with https://.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidInput("Do not put a username or password in the link. BAYAN never stores "
                           "library passwords.")
    host = parts.hostname
    if not host or "{" in parts.netloc or "}" in parts.netloc:
        raise InvalidInput("The placeholder belongs in the path or query, not in the host name.")
    if not _public_host(host):
        raise InvalidInput("The host must be a public address, not localhost or a private "
                           "network.")
    rest = template
    for p in PLACEHOLDERS:
        rest = rest.replace(p, "")
    if "{" in rest or "}" in rest:
        raise InvalidInput("Only {url} and {doi} are allowed as placeholders.")
    return template


def _public_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        return _is_public(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    # No DNS lookup here: BAYAN never connects to this host, it only builds a link.
    return "." in host and not host.endswith((".local", ".localhost", ".internal", ".lan",
                                              ".home", ".corp", ".intranet"))


def save_template(conn: sqlite3.Connection, user_id: int, template: str) -> str:
    updated = _now()
    conn.execute(
        "INSERT INTO membership_proxy(user_id, template, updated_at) VALUES(?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET template = excluded.template,"
        " updated_at = excluded.updated_at", (user_id, template, updated))
    conn.commit()
    return updated


def get_template(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT template, updated_at FROM membership_proxy WHERE user_id = ?",
                        (user_id,)).fetchone()


def delete_template(conn: sqlite3.Connection, user_id: int) -> bool:
    cur = conn.execute("DELETE FROM membership_proxy WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount > 0


def clean_doi(raw: str) -> str:
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", raw.strip(), flags=re.IGNORECASE)
    if not _DOI.match(doi) or len(doi) > 200:
        raise InvalidInput("That is not a valid DOI (it should look like 10.1000/abc123).")
    return doi


def clean_target_url(raw: str) -> str:
    url = raw.strip()
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018
    except ValueError as exc:
        raise InvalidInput("That is not a valid paper link.") from exc
    if (len(url) > MAX_TARGET_LENGTH or parts.scheme not in ("http", "https")
            or not parts.hostname or _BAD_CHARS.search(url)):
        raise InvalidInput("Give an http(s) paper link without spaces.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidInput("The paper link must not contain a username or password.")
    return url


def build_link(template: str, doi: str | None = None, url: str | None = None) -> str:
    """Fill the template for a paper. A DOI also stands in for {url} as https://doi.org/<doi>."""
    if doi:
        doi = clean_doi(doi)
    if url:
        url = clean_target_url(url)
    if not doi and not url:
        raise InvalidInput("Give a DOI or a paper link.")
    target = url or f"https://doi.org/{quote(doi, safe='/()')}"
    out = template
    if "{doi}" in out:
        if not doi:
            raise InvalidInput("Your template needs a DOI, but this paper has none.")
        out = out.replace("{doi}", quote(doi, safe="/"))
    return out.replace("{url}", quote(target, safe=""))


def paper_proxy_link(template: str | None, paper: dict) -> str | None:
    """Proxy link for a result row, or None when no template is saved or the paper has no
    usable DOI/link."""
    if not template:
        return None
    for kwargs in ({"doi": paper.get("doi")}, {"url": paper.get("landing_url")},
                   {"url": paper.get("oa_url")}):
        if not any(kwargs.values()):
            continue
        try:
            return build_link(template, **kwargs)
        except InvalidInput:
            continue
    return None
