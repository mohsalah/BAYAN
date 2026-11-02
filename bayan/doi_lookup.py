"""Automatic Crossref DOI lookup for papers found elsewhere without a DOI.

Deterministic code, sequential requests, a per-run budget (a count and a time limit) and a
cache (including "no match").
A Crossref failure is counted and never fails the run. A DOI is only ever filled in when the
paper has none, and only after the connector's confidence checks passed.
"""

import sqlite3
import time
from collections.abc import Callable
from typing import Protocol

from .connectors.base import ConnectorError
from .connectors.crossref import DoiMatch, normalize_text
from .models import Paper

DOI_SOURCE = "crossref_lookup"


class DoiLooker(Protocol):
    def lookup_doi(self, title: str, first_author: str, year: int) -> DoiMatch | None: ...


def cache_key(paper: Paper) -> str:
    return "|".join((normalize_text(paper.title), normalize_text(paper.first_author),
                     str(paper.year)))


class DoiLookup:
    def __init__(self, conn: sqlite3.Connection, looker: DoiLooker, budget: int,
                 seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._conn = conn
        self._looker = looker
        self._remaining = max(budget, 0)
        # Crossref title queries take ~3 s each, so 20 of them held a search for a minute
        # (measured by the M27 bench). `seconds` caps the time spent waiting on Crossref.
        self._seconds = seconds
        self._clock = clock
        self._spent = 0.0
        self.counts = {"attempted": 0, "matched": 0, "no_match": 0, "failed": 0,
                       "skipped_over_budget": 0, "skipped_over_time": 0, "cache_hits": 0}

    def __call__(self, paper: Paper) -> None:
        """Fill `paper.doi` in place if it has none and Crossref has a confident match."""
        if paper.doi or not paper.title.strip() or paper.year is None:
            return  # nothing to look up, or the year cannot be verified
        key = cache_key(paper)
        row = self._conn.execute(
            "SELECT doi, score FROM doi_lookup_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is not None:
            self.counts["cache_hits"] += 1
            if row["doi"]:
                self._apply(paper, DoiMatch(row["doi"], row["score"]))
            return
        if self._remaining <= 0:
            self.counts["skipped_over_budget"] += 1
            return
        if self._seconds is not None and self._spent >= self._seconds:
            self.counts["skipped_over_time"] += 1
            return
        self._remaining -= 1
        self.counts["attempted"] += 1
        started = self._clock()
        try:
            match = self._looker.lookup_doi(paper.title, paper.first_author, paper.year)
        except ConnectorError:
            self.counts["failed"] += 1  # not cached: a later run may succeed
            return
        finally:
            self._spent += self._clock() - started
        self._conn.execute(
            "INSERT OR REPLACE INTO doi_lookup_cache(cache_key, doi, score) VALUES(?,?,?)",
            (key, match.doi if match else "", match.score if match else None),
        )
        self._conn.commit()
        if match is None:
            self.counts["no_match"] += 1
            return
        self.counts["matched"] += 1
        self._apply(paper, match)

    @staticmethod
    def _apply(paper: Paper, match: DoiMatch) -> None:
        paper.doi = match.doi
        paper.doi_source = DOI_SOURCE
        paper.doi_match_score = match.score
