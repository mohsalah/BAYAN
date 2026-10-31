"""Connector interface. Every library (open or membership) implements this."""

import time
from collections.abc import Callable, Iterator
from typing import Protocol

import httpx

from ..models import Paper, SearchProfile

# Filters a SearchProfile can ask for (the period is always requested).
FILTERS = ("years", "document_types", "languages", "countries", "topics")
RETRY_STATUSES = (429, 500, 502, 503, 504)


class ConnectorError(RuntimeError):
    """Raised for failed requests. Messages never contain keys or request parameters."""


class Connector(Protocol):
    name: str
    tier: str  # "open" or "membership"
    # Subset of FILTERS the source can really apply. Anything else is reported, never guessed.
    supported_filters: frozenset[str]
    # Reference types (models.DOCUMENT_TYPES) the source can return. A connector without it
    # is reported as "capability not recorded", never assumed to supply a type.
    supported_categories: frozenset[str]

    def search(self, profile: SearchProfile) -> Iterator[Paper]: ...


MAX_RETRY_WAIT = 60.0  # seconds; the downloader caps its waits the same way


def get_with_retry(
    client: httpx.Client,
    url: str,
    params: dict,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 5,
    headers: dict | None = None,
) -> httpx.Response:
    """GET, retrying 429/5xx and transport errors; honours a numeric Retry-After.

    The error text carries only the library label and status, never the URL or parameters.
    """
    error = "unknown error"
    for attempt in range(1, max_attempts + 1):
        response = None
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            error = type(exc).__name__
        else:
            if response.status_code == 200:
                return response
            error = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUSES:
                break
        if attempt < max_attempts:
            retry_after = response.headers.get("Retry-After") if response is not None else None
            sleep(retry_wait(retry_after, attempt, label, error))
    raise ConnectorError(f"{label} request failed: {error}")


def retry_wait(retry_after: str | None, attempt: int, label: str, error: str) -> float:
    """Seconds to wait before the next try. A numeric Retry-After is honoured up to
    MAX_RETRY_WAIT; a longer one (a daily or hourly limit) fails the request at once, so a
    search reports the limit instead of hanging until it resets."""
    if retry_after is None or not retry_after.strip().isdigit():
        return 2.0 * attempt
    wait = float(retry_after)
    if wait > MAX_RETRY_WAIT:
        raise ConnectorError(f"{label} request failed: {error}; the library asks to wait "
                             f"{_duration(wait)} before the next request (a usage limit). "
                             "Try again later.")
    return wait


def _duration(seconds: float) -> str:
    if seconds >= 5400:
        return f"about {round(seconds / 3600)} hours"
    return f"about {max(1, round(seconds / 60))} minutes"


def user_agent(contact_email: str | None) -> str:
    return f"BAYAN/0.1 (mailto:{contact_email})" if contact_email else "BAYAN/0.1"
