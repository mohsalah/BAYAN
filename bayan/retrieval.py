"""Quota-aware collection with de-duplication. Deterministic; no LLM involved."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .models import Paper


@dataclass
class CollectResult:
    papers: list[Paper] = field(default_factory=list)
    duplicates: int = 0
    skipped_over_quota: int = 0
    skipped_no_year: int = 0
    # year -> (quota, collected) for every year that fell short. Never padded.
    shortfalls: dict[int, tuple[int, int]] = field(default_factory=dict)


def _key(paper: Paper) -> str:
    return f"doi:{paper.doi.lower()}" if paper.doi else f"{paper.source}:{paper.id}"


def merge_into(base: Paper, other: Paper) -> None:
    """Fold a duplicate into the kept paper: fill gaps, keep the richer value, keep base's DOI."""
    for name in ("title", "language", "venue", "first_author", "document_type", "oa_url",
                 "landing_url", "topic"):
        if not getattr(base, name) and getattr(other, name):
            setattr(base, name, getattr(other, name))
    if len(other.abstract) > len(base.abstract):
        base.abstract = other.abstract
    if base.year is None:
        base.year = other.year
    base.citations = max(base.citations, other.citations)
    base.is_oa = base.is_oa or other.is_oa
    base.countries = list(dict.fromkeys([*base.countries, *other.countries]))
    base.keywords = list(dict.fromkeys([*base.keywords, *other.keywords]))
    if not base.doi:
        base.doi, base.doi_source, base.doi_match_score = (
            other.doi, other.doi_source, other.doi_match_score)
    elif other.doi and not other.doi_source and base.doi_source:
        # A library gave the same DOI itself, so it is no longer only a lookup result.
        base.doi_source, base.doi_match_score = "", None


def collect(
    papers: Iterable[Paper],
    quotas: dict[int, int] | None = None,
    enrich: Callable[[Paper], None] | None = None,
) -> CollectResult:
    """Keep each work once (DOI, else source+id); with quotas, cap years and report shortfalls.

    `enrich` (e.g. the DOI lookup) runs only on a paper that would be kept, before it is
    counted, so a DOI it finds can still reveal a duplicate that must not use up a quota slot.
    """
    result = CollectResult()
    kept: dict[str, Paper] = {}
    counts = {y: 0 for y in quotas} if quotas else {}
    for paper in papers:
        key = _key(paper)
        if key in kept:
            result.duplicates += 1
            merge_into(kept[key], paper)
            continue
        if quotas is not None:
            if paper.year is None:
                result.skipped_no_year += 1
                continue
            if paper.year not in quotas or counts[paper.year] >= quotas[paper.year]:
                result.skipped_over_quota += 1
                continue
        if enrich is not None and not paper.doi:
            enrich(paper)
            key = _key(paper)
            if key in kept:
                result.duplicates += 1
                merge_into(kept[key], paper)
                continue
        if quotas is not None:
            counts[paper.year] += 1
        kept[key] = paper
        result.papers.append(paper)
        if quotas is not None and all(counts[y] >= q for y, q in quotas.items()):
            break
    if quotas is not None:
        result.shortfalls = {
            y: (q, counts[y]) for y, q in quotas.items() if counts[y] < q
        }
    return result
