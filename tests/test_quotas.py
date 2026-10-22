import json
from pathlib import Path

import pytest

from brilliant_research_agent.quotas import REFERENCE_BANDS, allocate_quotas, year_weights

REFERENCE_JSON = Path.home() / "Downloads/Reactor physics papers/year_quotas.json"


def reference_quotas():
    return allocate_quotas(25_000, year_weights(REFERENCE_BANDS))


def test_sums_exactly_to_total():
    assert sum(reference_quotas().values()) == 25_000


def test_period_totals_match_reference_project():
    q = reference_quotas()
    assert sum(v for y, v in q.items() if y <= 1999) == 8_450
    assert sum(v for y, v in q.items() if 2000 <= y <= 2009) == 5_070
    assert sum(v for y, v in q.items() if y >= 2010) == 11_480


def test_ties_favour_recent_years():
    q = reference_quotas()
    assert q[1950] == 169 and q[2000] == 507 and q[2010] == 675
    assert [y for y, v in q.items() if v == 676] == [2022, 2023, 2024, 2025, 2026]


@pytest.mark.skipif(not REFERENCE_JSON.exists(), reason="reference project not on this machine")
def test_matches_reference_json_exactly():
    expected = {int(y): n for y, n in json.loads(REFERENCE_JSON.read_text())["quotas"].items()}
    assert reference_quotas() == expected


def test_small_total_has_no_negative_or_lost_items():
    q = allocate_quotas(7, {2020: 1, 2021: 1, 2022: 1})
    assert sum(q.values()) == 7 and sorted(q.values()) == [2, 2, 3]


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        allocate_quotas(-1, {2020: 1})
    with pytest.raises(ValueError):
        allocate_quotas(10, {})
    with pytest.raises(ValueError):
        year_weights([(2000, 2005, 1), (2005, 2010, 1)])
