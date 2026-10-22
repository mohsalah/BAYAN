"""Per-year quota allocation (largest remainder), generalised from the reference project."""

from collections.abc import Iterable

# (first_year, last_year, weight) bands used by the reference Reactor Physics library:
# 200 for 2010-2026, 150 for 2000-2009, 50 for 1950-1999.
REFERENCE_BANDS: tuple[tuple[int, int, int], ...] = (
    (1950, 1999, 50),
    (2000, 2009, 150),
    (2010, 2026, 200),
)


def year_weights(bands: Iterable[tuple[int, int, int]]) -> dict[int, int]:
    weights: dict[int, int] = {}
    for first, last, weight in bands:
        if first > last or weight <= 0:
            raise ValueError(f"invalid band: {(first, last, weight)}")
        for year in range(first, last + 1):
            if year in weights:
                raise ValueError(f"year {year} is in more than one band")
            weights[year] = weight
    return weights


def allocate_quotas(total: int, weights: dict[int, int]) -> dict[int, int]:
    """Split `total` across years in proportion to `weights`, summing exactly to `total`.

    Largest remainder; ties go to the more recent year.
    """
    if total < 0:
        raise ValueError("total must not be negative")
    if not weights or any(w <= 0 for w in weights.values()):
        raise ValueError("weights must be non-empty and positive")
    weight_sum = sum(weights.values())
    base = {y: total * w // weight_sum for y, w in weights.items()}
    remainder = {y: total * w % weight_sum for y, w in weights.items()}
    left = total - sum(base.values())
    for year in sorted(weights, key=lambda y: (remainder[y], y), reverse=True)[:left]:
        base[year] += 1
    return base
