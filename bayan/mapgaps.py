"""Gap hints from the concept map (M19), computed from the co-occurrence graph. No model call.

The nodes are the run's merged concept nodes (M17). Two concepts are linked when at least one
chosen paper has verified mentions of both. That link is co-occurrence only, not a stated
relation (evidence-backed relations such as `contradicts` come with the map edges of M18). On
the graph:

- clusters: Louvain communities, seeded, so the same input always gives the same output;
- bridges: the concepts with the highest betweenness centrality, which join clusters;
- hints, each an unverified candidate with its concepts, papers, quotes and a follow-up query:
  `isolated` (concepts that appear in one chosen paper only and share nothing with the rest),
  `weakly_linked_clusters` (two clusters joined by at most one chosen paper),
  `never_together` (two well-supported concepts that no chosen paper studies together),
  `method_single_system` (a method used with only one system in the chosen set).

Every hint carries the same caveat: the map covers only the chosen papers whose extraction is
done, so "never together" means "not together in this set" until the broader search was run and
a counterevidence pass was recorded. Counts and pairs come from the database, never from a model.
"""

from itertools import combinations

import networkx as nx

STATUS = "unverified candidate"
CAVEAT_RELATION = "co-occurrence within one paper"
MIN_PAPERS = 2  # a concept takes part in pair and method hints from this many papers
MAX_MIN_PAPERS = 10
MAX_PER_KIND = 20
MAX_BRIDGES = 5
MENTIONS_PER_CONCEPT = 2
CLUSTER_NAMES = 5
KINDS = {
    "isolated": "Concepts found in one chosen paper only, sharing no concept with the others.",
    "weakly_linked_clusters": "Two clusters of concepts joined by at most one chosen paper.",
    "never_together": "Two well-supported concepts that no chosen paper studies together.",
    "method_single_system": "A method used with only one system in the chosen set.",
}

Ref = tuple[str, str]


def _ref(paper: dict) -> Ref:
    return (paper["source"], paper["id"])


def _paper(ref: Ref) -> dict:
    return {"source": ref[0], "id": ref[1]}


def _papers(refs) -> list[dict]:
    return [_paper(r) for r in sorted(set(refs))]


# --------------------------------------------------------------------------- graph

def build_graph(nodes: list[dict]) -> nx.Graph:
    """Concept nodes, linked when they share a paper. Edge `weight` counts the shared papers."""
    g = nx.Graph()
    by_paper: dict[Ref, list[str]] = {}
    for i, n in enumerate(nodes):
        refs = sorted({_ref(p) for p in n["papers"]})
        g.add_node(n["key"], index=i, name=n["name"], type=n["type"], papers=refs)
        for ref in refs:
            by_paper.setdefault(ref, []).append(n["key"])
    for ref, keys in by_paper.items():
        for a, b in combinations(keys, 2):
            if g.has_edge(a, b):
                g.edges[a, b]["weight"] += 1
                g.edges[a, b]["papers"].append(ref)
            else:
                g.add_edge(a, b, weight=1, papers=[ref])
    for _, _, data in g.edges(data=True):
        data["papers"].sort()
    return g


def _clusters(g: nx.Graph) -> list[list[str]]:
    if g.number_of_nodes() == 0:
        return []
    found = nx.community.louvain_communities(g, weight="weight", seed=0)
    return sorted((sorted(c) for c in found), key=lambda c: (-len(c), c))


def _cluster_papers(g: nx.Graph, keys: list[str]) -> set[Ref]:
    return set().union(*(set(g.nodes[k]["papers"]) for k in keys)) if keys else set()


# --------------------------------------------------------------------------- hint parts

def _concept(node: dict) -> dict:
    """A concept as shown inside a hint: its papers and a few verified quotes, one per paper."""
    mentions, seen = [], set()
    for m in node["mentions"]:
        ref = _ref(m["paper"])
        if ref in seen:
            continue
        seen.add(ref)
        mentions.append({"paper": _paper(ref), "label": m["label"], "quote": m["quote"],
                         "page": m["page"], "page_label": m["page_label"], "level": m["level"],
                         "language": m["language"]})
        if len(mentions) == MENTIONS_PER_CONCEPT:
            break
    return {"key": node["key"], "name": node["name"], "type": node["type"],
            "paper_count": node["paper_count"], "papers": _papers(_ref(p) for p in node["papers"]),
            "mentions": mentions}


def _hint(kind: str, claim: str, concepts: list[dict], papers, query: str, note: str,
          caveat: str, **extra) -> dict:
    return {"kind": kind, "status": STATUS, "claim": claim,
            "concepts": [_concept(n) for n in concepts], "papers": _papers(papers),
            "follow_up": {"query": query, "note": note}, "caveat": caveat, **extra}


def _quoted(*names: str) -> str:
    return " AND ".join(f'"{n}"' for n in names)


def _names(nodes: list[dict]) -> str:
    return ", ".join(f'"{n["name"]}"' for n in nodes)


def _isolated(g: nx.Graph, by_key: dict[str, dict], caveat: str) -> list[dict]:
    out = []
    for comp in nx.connected_components(g):
        keys = sorted(comp, key=lambda k: g.nodes[k]["index"])
        papers = _cluster_papers(g, keys)
        if len(papers) != 1:
            continue
        ref = next(iter(papers))
        nodes = [by_key[k] for k in keys]
        out.append(_hint(
            "isolated",
            f"{_names(nodes)}: found in one chosen paper only ({ref[1]}) and sharing no concept "
            f"with any other chosen paper.",
            nodes, papers, _quoted(*(n["name"] for n in nodes[:2])),
            "Search beyond the chosen set for other papers on these concepts; isolation in the "
            "map may only mean the set is small.", caveat))
    out.sort(key=lambda h: (-len(h["concepts"]), h["papers"][0]["id"]))
    return out[:MAX_PER_KIND]


def _weak_clusters(g: nx.Graph, clusters: list[list[str]], by_key: dict[str, dict],
                   caveat: str, min_papers: int) -> list[dict]:
    members = [c for c in clusters if len(c) >= 2 and len(_cluster_papers(g, c)) >= min_papers]
    ids = {tuple(c): i for i, c in enumerate(clusters)}
    paper_keys: dict[Ref, dict[int, set[str]]] = {}
    for c in members:
        for k in c:
            for ref in g.nodes[k]["papers"]:
                paper_keys.setdefault(ref, {}).setdefault(ids[tuple(c)], set()).add(k)
    out = []
    for a, b in combinations(members, 2):
        ia, ib = ids[tuple(a)], ids[tuple(b)]
        bridging = sorted(ref for ref, by in paper_keys.items() if ia in by and ib in by)
        if len(bridging) > 1:
            continue
        top_a = sorted(a, key=lambda k: -by_key[k]["paper_count"])[:CLUSTER_NAMES]
        top_b = sorted(b, key=lambda k: -by_key[k]["paper_count"])[:CLUSTER_NAMES]
        na, nb = [by_key[k] for k in top_a], [by_key[k] for k in top_b]
        if bridging:
            how = f"only one chosen paper links them ({bridging[0][1]})"
        else:
            how = "not together in this set: no chosen paper links them"
        out.append(_hint(
            "weakly_linked_clusters",
            f"Cluster {ia + 1} ({_names(na)}) and cluster {ib + 1} ({_names(nb)}) are {how}.",
            na[:3] + nb[:3], _cluster_papers(g, a) | _cluster_papers(g, b),
            _quoted(na[0]["name"], nb[0]["name"]),
            "Search beyond the chosen set for papers that treat both clusters together; the "
            "hint stands only if none is found.", caveat,
            clusters=[{"id": ia, "concepts": a}, {"id": ib, "concepts": b}],
            bridging_papers=[{**_paper(ref), "concepts": sorted(paper_keys[ref][ia]
                                                                | paper_keys[ref][ib])}
                             for ref in bridging]))
    out.sort(key=lambda h: (len(h["bridging_papers"]), -len(h["papers"])))
    return out[:MAX_PER_KIND]


def _never_together(g: nx.Graph, nodes: list[dict], caveat: str, min_papers: int) -> list[dict]:
    strong = [n for n in nodes if n["paper_count"] >= min_papers]
    pairs = []
    for a, b in combinations(strong, 2):
        if g.has_edge(a["key"], b["key"]):
            continue
        first, second = sorted((a, b), key=lambda n: (-n["paper_count"], n["key"]))
        pairs.append((-min(a["paper_count"], b["paper_count"]),
                      -a["paper_count"] * b["paper_count"], first["key"], second["key"],
                      first, second))
    pairs.sort(key=lambda p: p[:4])
    out = []
    for *_, a, b in pairs[:MAX_PER_KIND]:
        out.append(_hint(
            "never_together",
            f'"{a["name"]}" ({a["paper_count"]} papers) and "{b["name"]}" ({b["paper_count"]} '
            f"papers) are never studied together in this set.",
            [a, b], [_ref(p) for n in (a, b) for p in n["papers"]],
            _quoted(a["name"], b["name"]),
            "Search beyond the chosen set for papers that study both; the hint stands only if "
            "none is found.", caveat))
    return out


def _single_system(g: nx.Graph, nodes: list[dict], by_key: dict[str, dict], caveat: str,
                   min_papers: int) -> list[dict]:
    out = []
    for n in nodes:
        if n["type"] != "method" or n["paper_count"] < min_papers:
            continue
        systems = sorted(k for k in g.neighbors(n["key"]) if g.nodes[k]["type"] == "system")
        if len(systems) != 1:
            continue
        system = by_key[systems[0]]
        out.append(_hint(
            "method_single_system",
            f'"{n["name"]}" is used with only one system in this set ("{system["name"]}", '
            f'{n["paper_count"]} papers).',
            [n, system], g.nodes[n["key"]]["papers"],
            f'"{n["name"]}" NOT "{system["name"]}"',
            "Search beyond the chosen set for the method applied to other systems; the hint "
            "stands only if nothing is found.", caveat))
    out.sort(key=lambda h: (-h["concepts"][0]["paper_count"], h["concepts"][0]["key"]))
    return out[:MAX_PER_KIND]


# --------------------------------------------------------------------------- coverage

def _coverage(nodes: list[dict], papers_done: list[Ref]) -> dict:
    levels: dict[Ref, str] = {}
    for n in nodes:
        for m in n["mentions"]:
            ref = _ref(m["paper"])
            if m["level"] == "full_text":
                levels[ref] = "full_text"
            else:
                levels.setdefault(ref, "abstract")
    done = set(papers_done)
    full = sum(1 for r in done if levels.get(r) == "full_text")
    abstract = sum(1 for r in done if levels.get(r) == "abstract")
    return {"papers": len(done), "full_text": full, "abstract_only": abstract,
            "without_concepts": len(done) - full - abstract, "concepts": len(nodes)}


def caveat_for(coverage: dict) -> str:
    return (f"These hints cover only the {coverage['papers']} chosen papers whose concept "
            f"extraction is done ({coverage['abstract_only']} abstract-only, "
            f"{coverage['without_concepts']} with no verified concept). Links are "
            f"{CAVEAT_RELATION}, not stated relations. \"Never together\" and \"only one "
            f"system\" mean within this set: each hint stays an unverified candidate until a "
            f"broader search and a counterevidence pass were recorded.")


# --------------------------------------------------------------------------- entry point

def analyse(nodes: list[dict], papers_done: list[Ref], min_papers: int = MIN_PAPERS) -> dict:
    """Clusters, bridges, co-occurrence edges and gap hints for the run's concept nodes.

    `nodes` is what `concepts.build_nodes` returns; `papers_done` are the chosen papers whose
    extraction finished (including those with no verified concept, for the coverage counts)."""
    coverage = _coverage(nodes, papers_done)
    caveat = caveat_for(coverage)
    g = build_graph(nodes)
    by_key = {n["key"]: n for n in nodes}
    clusters = _clusters(g)
    cluster_of = {k: i for i, c in enumerate(clusters) for k in c}
    betweenness = nx.betweenness_centrality(g) if g.number_of_edges() else dict.fromkeys(g, 0.0)

    bridges = []
    for k in sorted(g, key=lambda k: (-betweenness[k], g.nodes[k]["index"])):
        if betweenness[k] <= 0 or len(bridges) == MAX_BRIDGES:
            break
        bridges.append({"key": k, "name": g.nodes[k]["name"],
                        "betweenness": round(betweenness[k], 4),
                        "clusters": sorted({cluster_of[x] for x in g.neighbors(k)})})
    edges = []
    for u, v, data in g.edges(data=True):
        a, b = sorted((u, v), key=lambda k: g.nodes[k]["index"])
        edges.append({"a": a, "b": b, "relation": "co-occurs-with", "weight": data["weight"],
                      "papers": _papers(data["papers"])})
    edges.sort(key=lambda e: (g.nodes[e["a"]]["index"], g.nodes[e["b"]]["index"]))

    hints = (_isolated(g, by_key, caveat)
             + _weak_clusters(g, clusters, by_key, caveat, min_papers)
             + _never_together(g, nodes, caveat, min_papers)
             + _single_system(g, nodes, by_key, caveat, min_papers))
    return {
        "status": STATUS, "caveat": caveat, "coverage": coverage, "min_papers": min_papers,
        "kinds": KINDS,
        "nodes": [{"key": n["key"], "name": n["name"], "type": n["type"],
                   "paper_count": n["paper_count"], "cluster": cluster_of[n["key"]],
                   "degree": g.degree[n["key"]],
                   "betweenness": round(betweenness[n["key"]], 4)} for n in nodes],
        "clusters": [{"id": i, "concepts": c,
                      "names": [by_key[k]["name"] for k in
                                sorted(c, key=lambda k: -by_key[k]["paper_count"])[:CLUSTER_NAMES]],
                      "papers": _papers(_cluster_papers(g, c))} for i, c in enumerate(clusters)],
        "bridges": bridges,
        "edges": edges,
        "hints": hints,
    }


__all__ = ["CAVEAT_RELATION", "KINDS", "MAX_MIN_PAPERS", "MIN_PAPERS", "STATUS", "analyse",
           "build_graph", "caveat_for"]
