"""Automatic Crossref DOI lookup for papers found elsewhere without a DOI.

Deterministic code, sequential requests, a per-run budget and a cache (including "no match").
A Crossref failure is counted and never fails the run. A DOI is only ever filled in when the
paper has none, and only after the connector's confidence checks passed.
"""

import sqlite3
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
    def __init__(self, conn: sqlite3.Connection, looker: DoiLooker, budget: int) -> None:
        self._conn = conn
        self._looker = looker
        self._remaining = max(budget, 0)
        self.counts = {"attempted": 0, "matched": 0, "no_match": 0, "failed": 0,
                       "skipped_over_budget": 0, "cache_hits": 0}

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
        self._remaining -= 1
        self.counts["attempted"] += 1
        try:
            match = self._looker.lookup_doi(paper.title, paper.first_author, paper.year)
        except ConnectorError:
            self.counts["failed"] += 1  # not cached: a later run may succeed
            return
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
