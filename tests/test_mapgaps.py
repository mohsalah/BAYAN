"""M19: gap hints computed from the concept co-occurrence graph. Deterministic, no model call."""

import pytest
from fastapi.testclient import TestClient
from test_accounts_search import register
from test_concepts import env, extract  # noqa: F401  (fixture re-used)

from bayan.mapgaps import (
    CAVEAT_RELATION,
    STATUS,
    analyse,
    build_graph,
)


def node(key, ctype, papers, name=None):
    """A minimal concept node as build_nodes() returns it: one abstract mention per paper."""
    refs = [{"source": "openalex", "id": p} for p in papers]
    return {"key": key, "name": name or key, "type": ctype, "types": {ctype: len(papers)},
            "members": [key], "merged_by": [], "labels": [], "languages": ["en"],
            "paper_count": len(papers), "papers": refs,
            "mentions": [{"paper": r, "label": key, "quote": f"... {key} ...", "page": None,
                          "page_label": None, "level": "abstract", "language": "en"}
                         for r in refs]}


def done(*ids):
    return [("openalex", i) for i in ids]


def kinds(result):
    return {h["kind"] for h in result["hints"]}


def hint(result, kind):
    return [h for h in result["hints"] if h["kind"] == kind]


# ---------------------------------------------------------------- graph

def test_edges_are_shared_papers_only():
    nodes = [node("a", "method", ["P1", "P2"]), node("b", "system", ["P2", "P3"]),
             node("c", "quantity", ["P4"])]
    g = build_graph(nodes)
    assert set(g.nodes) == {"a", "b", "c"}
    assert list(g.edges) == [("a", "b")]
    assert g.edges["a", "b"]["weight"] == 1 and g.edges["a", "b"]["papers"] == [("openalex", "P2")]
    assert g.degree["c"] == 0


def test_empty_set_gives_no_hints_but_the_caveat():
    r = analyse([], done())
    assert r["hints"] == [] and r["clusters"] == [] and r["edges"] == []
    assert r["coverage"]["papers"] == 0 and r["status"] == STATUS
    assert "only" in r["caveat"] and CAVEAT_RELATION in r["caveat"]


# ---------------------------------------------------------------- hint kinds

def test_isolated_concepts_come_from_a_single_unconnected_paper():
    nodes = [node("a", "method", ["P1", "P2"]), node("b", "system", ["P1", "P2"]),
             node("lonely", "result", ["P3"]), node("other", "quantity", ["P3"])]
    r = analyse(nodes, done("P1", "P2", "P3"))
    iso = hint(r, "isolated")
    assert len(iso) == 1
    assert {c["key"] for c in iso[0]["concepts"]} == {"lonely", "other"}
    assert iso[0]["papers"] == [{"source": "openalex", "id": "P3"}]
    assert "P3" in iso[0]["claim"] or "one paper" in iso[0]["claim"]


def test_weakly_linked_clusters_count_bridging_papers():
    left = [node(k, "method", ["P1", "P2"]) for k in ("a1", "a2", "a3")]
    right = [node(k, "system", ["P3", "P4"]) for k in ("b1", "b2", "b3")]
    # never bridged: no paper mentions a concept of both groups
    r = analyse(left + right, done("P1", "P2", "P3", "P4"))
    weak = hint(r, "weakly_linked_clusters")
    assert len(weak) == 1 and weak[0]["bridging_papers"] == []
    assert len(r["clusters"]) == 2
    assert "not together in this set" in weak[0]["claim"]

    # one bridging paper: still a hint, and the bridge is named with its concepts
    bridge = [node("a1", "method", ["P1", "P2", "P5"]), node("b1", "system", ["P3", "P4", "P5"])]
    nodes = bridge + left[1:] + right[1:]
    r = analyse(nodes, done("P1", "P2", "P3", "P4", "P5"))
    weak = hint(r, "weakly_linked_clusters")
    assert len(weak) == 1
    assert weak[0]["bridging_papers"] == [{"source": "openalex", "id": "P5",
                                           "concepts": ["a1", "b1"]}]
    assert weak[0]["follow_up"]["query"]

    # two bridging papers: the clusters are linked, no hint
    bridge = [node("a1", "method", ["P1", "P2", "P5", "P6"]),
              node("b1", "system", ["P3", "P4", "P5", "P6"])]
    r = analyse(bridge + left[1:] + right[1:], done("P1", "P2", "P3", "P4", "P5", "P6"))
    assert hint(r, "weakly_linked_clusters") == []


def test_never_together_needs_min_papers_each_and_ranks_by_support():
    nodes = [node("big", "method", ["P1", "P2", "P3"]), node("mid", "system", ["P4", "P5"]),
             node("small", "system", ["P6"]), node("with_big", "quantity", ["P1"])]
    r = analyse(nodes, done("P1", "P2", "P3", "P4", "P5", "P6"))
    pairs = [(h["concepts"][0]["key"], h["concepts"][1]["key"])
             for h in hint(r, "never_together")]
    assert pairs == [("big", "mid")]  # "small" and "with_big" have one paper each
    h = hint(r, "never_together")[0]
    assert '"big" AND "mid"' in h["follow_up"]["query"]
    assert "in this set" in h["claim"]

    # a lower threshold admits the single-paper concepts, best supported pair first
    r = analyse(nodes, done("P1", "P2", "P3", "P4", "P5", "P6"), min_papers=1)
    pairs = [(h["concepts"][0]["key"], h["concepts"][1]["key"])
             for h in hint(r, "never_together")]
    assert pairs[0] == ("big", "mid") and ("big", "with_big") not in pairs
    assert ("big", "small") in pairs and ("mid", "small") in pairs


def test_method_validated_on_one_system_only():
    nodes = [node("code x", "method", ["P1", "P2"]), node("reactor a", "system", ["P1", "P2"]),
             node("code y", "method", ["P3", "P4"]), node("reactor b", "system", ["P3"]),
             node("reactor c", "system", ["P4"]),
             node("code z", "method", ["P5", "P6"])]  # no system named at all
    r = analyse(nodes, done("P1", "P2", "P3", "P4", "P5", "P6"))
    single = hint(r, "method_single_system")
    assert [h["concepts"][0]["key"] for h in single] == ["code x"]
    assert single[0]["concepts"][1]["key"] == "reactor a"
    assert single[0]["papers"] == [{"source": "openalex", "id": "P1"},
                                   {"source": "openalex", "id": "P2"}]
    assert "NOT" in single[0]["follow_up"]["query"]


# ---------------------------------------------------------------- common shape

def test_every_hint_is_a_candidate_with_evidence_and_a_follow_up():
    nodes = [node("a", "method", ["P1", "P2"]), node("s", "system", ["P1", "P2"]),
             node("b", "system", ["P3", "P4"]), node("c", "method", ["P3", "P4"]),
             node("lonely", "result", ["P5"])]
    r = analyse(nodes, done("P1", "P2", "P3", "P4", "P5"))
    assert kinds(r) >= {"isolated", "weakly_linked_clusters", "never_together",
                        "method_single_system"}
    for h in r["hints"]:
        assert h["status"] == STATUS
        assert h["claim"] and h["caveat"] == r["caveat"]
        assert h["concepts"] and all(c["mentions"] for c in h["concepts"])
        for c in h["concepts"]:
            for m in c["mentions"]:
                assert m["quote"] and m["paper"]["id"] and m["level"] == "abstract"
        assert h["papers"]
        assert h["follow_up"]["query"] and h["follow_up"]["note"]
    assert r["coverage"] == {"papers": 5, "full_text": 0, "abstract_only": 5,
                             "without_concepts": 0, "concepts": 5}


def test_structure_lists_clusters_bridges_and_co_occurrence_edges():
    left = [node(k, "method", ["P1", "P2"]) for k in ("a1", "a2")]
    right = [node(k, "system", ["P3", "P4"]) for k in ("b1", "b2")]
    bridge = [node("hub", "quantity", ["P2", "P3"])]
    r = analyse(left + right + bridge, done("P1", "P2", "P3", "P4"))
    assert len(r["clusters"]) == 2
    assert all(c["concepts"] and c["papers"] for c in r["clusters"])
    assert r["bridges"][0]["key"] == "hub" and r["bridges"][0]["betweenness"] > 0
    by_key = {n["key"]: n for n in r["nodes"]}
    assert by_key["hub"]["degree"] == 4 and by_key["a1"]["cluster"] != by_key["b1"]["cluster"]
    assert all(e["relation"] == "co-occurs-with" for e in r["edges"])
    assert {"a": "a1", "b": "hub", "relation": "co-occurs-with", "weight": 1,
            "papers": [{"source": "openalex", "id": "P2"}]} in r["edges"]


def test_result_is_deterministic():
    nodes = [node(f"c{i}", "method" if i % 2 else "system",
                  [f"P{j}" for j in range(i % 4, i % 4 + 3)]) for i in range(12)]
    papers = done(*[f"P{j}" for j in range(7)])
    assert analyse(nodes, papers) == analyse(nodes, papers)


def test_coverage_counts_papers_without_verified_concepts_and_full_text():
    n = node("a", "method", ["P1", "P2"])
    n["mentions"][0]["level"] = "full_text"
    n["mentions"][0]["page"] = 3
    r = analyse([n], done("P1", "P2", "P3"))
    assert r["coverage"] == {"papers": 3, "full_text": 1, "abstract_only": 1,
                             "without_concepts": 1, "concepts": 1}
    assert "3 chosen papers" in r["caveat"] and "1 with no verified concept" in r["caveat"]


# ---------------------------------------------------------------- API

def test_gap_hints_endpoint_uses_the_run_s_merged_nodes(env):  # noqa: F811
    e = env()
    extract(e, "W1", "W2", "W3", "W4")
    e.client.post("/api/concepts/normalize", json={"run_id": e.run_id})
    r = e.client.get("/api/concepts/gaps", params={"run_id": e.run_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == e.run_id and body["status"] == STATUS
    assert body["coverage"] == {"papers": 4, "full_text": 0, "abstract_only": 4,
                                "without_concepts": 0, "concepts": 2}
    assert {n["key"] for n in body["nodes"]} == {"molten salt reactor", "coupled code"}
    # one paper per concept pair at most: nothing reaches the default threshold of two papers
    assert body["hints"] == []

    r = e.client.get("/api/concepts/gaps", params={"run_id": e.run_id, "min_papers": 1})
    hints = r.json()["hints"]
    single = [h for h in hints if h["kind"] == "method_single_system"]
    assert len(single) == 1
    assert [c["key"] for c in single[0]["concepts"]] == ["coupled code", "molten salt reactor"]
    quote = single[0]["concepts"][0]["mentions"][0]["quote"]
    assert "coupled code" in quote and single[0]["concepts"][0]["mentions"][0]["level"] == "abstract"
    assert all(h["status"] == STATUS and h["caveat"] for h in hints)


def test_gap_hints_are_private_and_validated(env):  # noqa: F811
    e = env()
    extract(e, "W1")
    with TestClient(e.app) as other:
        register(other, "other@example.org")
        assert other.get("/api/concepts/gaps", params={"run_id": e.run_id}).status_code == 404
    r = e.client.get("/api/concepts/gaps", params={"run_id": e.run_id, "min_papers": 0})
    assert r.status_code == 422
    r = e.client.get("/api/concepts/gaps", params={"run_id": e.run_id, "min_papers": 99})
    assert r.status_code == 422


def test_run_without_extraction_has_empty_graph(env):  # noqa: F811
    e = env()
    r = e.client.get("/api/concepts/gaps", params={"run_id": e.run_id}).json()
    assert r["nodes"] == [] and r["hints"] == [] and r["coverage"]["papers"] == 0


@pytest.mark.parametrize("min_papers", [1, 2, 3])
def test_min_papers_never_drops_the_caveat(min_papers):
    r = analyse([node("a", "method", ["P1"])], done("P1"), min_papers=min_papers)
    assert r["caveat"] and r["status"] == STATUS
