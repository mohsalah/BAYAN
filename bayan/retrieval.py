"""Quota-aware collection with de-duplication. Deterministic; no LLM involved."""

from collections.abc import Iterable
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


def collect(papers: Iterable[Paper], quotas: dict[int, int] | None = None) -> CollectResult:
    """Keep each work once (DOI, else source+id); with quotas, cap years and report shortfalls."""
    result = CollectResult()
    seen: set[str] = set()
    counts = {y: 0 for y in quotas} if quotas else {}
    for paper in papers:
        key = f"doi:{paper.doi.lower()}" if paper.doi else f"{paper.source}:{paper.id}"
        if key in seen:
            result.duplicates += 1
            continue
        if quotas is not None:
            if paper.year is None:
                result.skipped_no_year += 1
                continue
            if paper.year not in quotas or counts[paper.year] >= quotas[paper.year]:
                result.skipped_over_quota += 1
                continue
            counts[paper.year] += 1
        seen.add(key)
        result.papers.append(paper)
        if quotas is not None and all(counts[y] >= q for y, q in quotas.items()):
            break
    if quotas is not None:
        result.shortfalls = {
            y: (q, counts[y]) for y, q in quotas.items() if counts[y] < q
        }
    return result
