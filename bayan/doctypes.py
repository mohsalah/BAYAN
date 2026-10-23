"""Per-type counts for a run: every selected reference type is listed, with 0 when empty.

Which library can supply a type comes from the connector's own `supported_categories`
(what its classifier can return). A library without that attribute is reported as
"capability not recorded"; nothing is guessed. User-supplied manuals are a separate row.
"""

from .connectors import ArxivConnector, CrossrefConnector, EuropePMCConnector, OpenAlexConnector
from .libraries import LIBRARIES
from .models import CATEGORY_INFO, DOCUMENT_TYPES, MANUAL_KIND, normalize_document_types

# Row id of the manuals row; every other row id is a reference-type id.
MANUAL_ROW = MANUAL_KIND

CAPABILITIES: dict[str, frozenset[str]] = {
    cls.name: cls.supported_categories
    for cls in (OpenAlexConnector, CrossrefConnector, ArxivConnector, EuropePMCConnector)
}
_LABELS = {c["id"]: c["label"] for c in CATEGORY_INFO}
_LIBRARY_NAMES = {lib["id"]: lib["name"] for lib in LIBRARIES}


def type_key(paper: dict) -> str:
    """The row a paper is counted under: 'manual' for manuals, else its category."""
    if paper.get("document_kind") == MANUAL_KIND:
        return MANUAL_ROW
    return paper.get("category", "other")


def _names(ids: list[str]) -> str:
    names = [_LIBRARY_NAMES.get(i, i) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _lower(label: str) -> str:
    return label[0].lower() + label[1:]


def type_report(selected: list[str], papers: list[dict], libraries: list[str]) -> dict:
    """Counts and plain-language sentences per reference type for one run.

    `selected` are the reference types the user chose; `libraries` the library ids searched.
    """
    selected = list(dict.fromkeys(normalize_document_types(selected) or DOCUMENT_TYPES))
    counts: dict[str, int] = {}
    by_lib: dict[str, dict[str, int]] = {}
    for p in papers:
        key = type_key(p)
        counts[key] = counts.get(key, 0) + 1
        if key != MANUAL_ROW:
            lib = by_lib.setdefault(key, {})
            lib[p["source"]] = lib.get(p["source"], 0) + 1
    # Types the user selected come first (in their order); a type with papers but not selected
    # (older runs) is still shown so no paper is uncounted.
    ids = selected + [k for k in DOCUMENT_TYPES if k not in selected and counts.get(k)]
    rows = []
    for key in ids:
        label = _LABELS.get(key, key)
        n = counts.get(key, 0)
        can = [lib for lib in libraries if key in CAPABILITIES.get(lib, ())]
        unknown = [lib for lib in libraries if lib not in CAPABILITIES]
        row = {"id": key, "label": label, "count": n, "selected": key in selected,
               "by_library": by_lib.get(key, {}), "can_supply": can,
               "capability_unknown": unknown}
        if n:
            split = ", ".join(f"{_LIBRARY_NAMES.get(s, s)} {c}"
                              for s, c in row["by_library"].items())
            row["message"] = f"{label}: {n} found ({split})."
        elif can:
            row["message"] = (f"{label}: 0 found in the chosen libraries ({_names(can)} can list "
                              f"{_lower(label)}, but none matched this search).")
        elif unknown:
            row["message"] = (f"{label}: 0 found in the chosen libraries (no selected library is "
                              f"recorded as listing {_lower(label)}; the capability of "
                              f"{_names(unknown)} is not recorded).")
        else:
            row["message"] = (f"{label}: 0 found in the chosen libraries (no selected library "
                              f"lists {_lower(label)}).")
        rows.append(row)
    n = counts.get(MANUAL_ROW, 0)
    rows.append({
        "id": MANUAL_ROW, "label": "Manuals (user-supplied)", "count": n, "selected": False,
        "by_library": {}, "can_supply": [], "capability_unknown": [],
        "message": (f"Manuals (user-supplied): {n} added by you." if n else
                    "Manuals (user-supplied): 0. No library is searched for manuals; add one "
                    "by URL or upload."),
    })
    return {"total": len(papers), "rows": rows}
