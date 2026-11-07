"""Citation-network metrics for a collection (M31). Deterministic, from stored references only.

Nodes are OpenAlex W-ids. An edge `cited -> citing` follows the flow of knowledge from the older
work to the newer one. Only references between two works of the collection count.

- Local citation score (LCS): how many collection works cite a work.
- Search-path count (SPC, Batagelj 2003, used by Hummon & Doreian's main-path analysis): the
  number of source-to-sink paths through an edge, N-(u) * N+(v), where N-(u) counts the paths
  from any source (no incoming edge) to u and N+(v) the paths from v to any sink.
- Main path, key-route search (Liu & Lu 2012): start from each of the top-k SPC edges and extend
  backwards and forwards, always along the adjacent edge with the highest SPC, until a source and
  a sink are reached. Ties go to the lower W-id so the result is reproducible.
- Citation graphs can hold cycles (errata, simultaneous papers). Works are ordered by
  (publication year, W-id) and an edge is kept only when it goes from earlier to later in that
  order, which removes every cycle; dropped edges are counted. Works without a year take part in
  LCS but not in the main path.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class Graph:
    nodes: dict[str, int | None]  # W-id -> publication year
    edges: list[tuple[str, str]] = field(default_factory=list)  # (cited, citing), deduplicated


def build_graph(nodes: dict[str, int | None], references: Iterable[tuple[str, str]]) -> Graph:
    """`references` are (citing, cited) pairs; only pairs inside `nodes` are kept."""
    edges = sorted({(cited, citing) for citing, cited in references
                    if citing in nodes and cited in nodes and citing != cited})
    return Graph(nodes, edges)


def local_citations(graph: Graph) -> dict[str, int]:
    lcs = dict.fromkeys(graph.nodes, 0)
    for cited, _ in graph.edges:
        lcs[cited] += 1
    return lcs


def _order(graph: Graph, w: str) -> tuple[int, str]:
    return (graph.nodes[w], _id_key(w))  # type: ignore[return-value]


def _id_key(w: str) -> str:
    # W2 before W10: compare by number when the ID is W + digits.
    return f"{int(w[1:]):020d}" if w[1:].isdigit() else w


def dag_edges(graph: Graph) -> tuple[list[tuple[str, str]], int]:
    """Edges kept for path analysis (earlier -> later), and how many were dropped."""
    kept = [(u, v) for u, v in graph.edges
            if graph.nodes[u] is not None and graph.nodes[v] is not None
            and _order(graph, u) < _order(graph, v)]
    return kept, len(graph.edges) - len(kept)


def search_path_counts(edges: list[tuple[str, str]], order: dict[str, tuple]) -> dict:
    """SPC of every edge of a DAG whose nodes are topologically ordered by `order`."""
    preds: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for u, v in edges:
        succs[u].append(v)
        preds[v].append(u)
    nodes = sorted({n for e in edges for n in e}, key=order.__getitem__)
    n_minus = {}
    for n in nodes:  # paths from any source to n
        n_minus[n] = sum(n_minus[p] for p in preds[n]) if preds[n] else 1
    n_plus = {}
    for n in reversed(nodes):  # paths from n to any sink
        n_plus[n] = sum(n_plus[s] for s in succs[n]) if succs[n] else 1
    return {(u, v): n_minus[u] * n_plus[v] for u, v in edges}


def main_paths(graph: Graph, key_routes: int = 1) -> dict:
    """Key-route main paths from the `key_routes` edges with the highest SPC."""
    edges, dropped = dag_edges(graph)
    if not edges:
        return {"routes": [], "spc": {}, "dropped_edges": dropped, "edges": 0}
    order = {w: _order(graph, w) for w in graph.nodes if graph.nodes[w] is not None}
    spc = search_path_counts(edges, order)
    preds: dict[str, list[str]] = defaultdict(list)
    succs: dict[str, list[str]] = defaultdict(list)
    for u, v in edges:
        succs[u].append(v)
        preds[v].append(u)

    def best(options: list[tuple[tuple[str, str], str]]) -> str:
        return min(options, key=lambda o: (-spc[o[0]], _id_key(o[1])))[1]

    ranked = sorted(spc, key=lambda e: (-spc[e], _id_key(e[0]), _id_key(e[1])))
    routes: list[list[str]] = []
    for u, v in ranked[:max(1, key_routes)]:
        back = [u]
        while preds[back[-1]]:
            n = back[-1]
            back.append(best([((p, n), p) for p in preds[n]]))
        forward = [v]
        while succs[forward[-1]]:
            n = forward[-1]
            forward.append(best([((n, s), s) for s in succs[n]]))
        route = list(reversed(back)) + forward
        if route not in routes:
            routes.append(route)
    return {"routes": routes, "spc": spc, "dropped_edges": dropped, "edges": len(edges)}


def recent_velocity(counts_by_year: list[dict] | None, current_year: int,
                    recent: int) -> float | None:
    """Mean citations per year over the last `recent` full years (a year not listed counts 0).
    None when the yearly counts were never stored."""
    if counts_by_year is None:
        return None
    by_year = {c["year"]: c["cited_by_count"] for c in counts_by_year}
    years = range(current_year - recent, current_year)
    return sum(by_year.get(y, 0) for y in years) / max(1, recent)


def velocity(citations: int | None, year: int | None, current_year: int) -> float | None:
    if year is None:
        return None
    return (citations or 0) / max(1, current_year - year + 1)
