"""Connector interface. Every library (open or membership) implements this."""

from collections.abc import Iterator
from typing import Protocol

from ..models import Paper, SearchProfile


class ConnectorError(RuntimeError):
    """Raised for failed requests. Messages never contain keys or request parameters."""


class Connector(Protocol):
    name: str
    tier: str  # "open" or "membership"

    def search(self, profile: SearchProfile) -> Iterator[Paper]: ...
