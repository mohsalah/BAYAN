"""Field taxonomy from OpenAlex: 4 domains, 26 fields, about 250 subfields.

Each subfield gets search queries taken from the names of its own largest OpenAlex topics, so
the queries are the field's vocabulary, not words we made up. Each field is also mapped to the
six OECD Frascati groups; that mapping is ours and approximate, and the file says so.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from .client import Client

API = "https://api.openalex.org"
QUERIES_PER_SUBFIELD = 10

# OpenAlex field -> OECD Frascati (FORD) group. Approximate: OpenAlex fields do not split along
# the Frascati lines everywhere (e.g. "Agricultural and Biological Sciences").
FRASCATI = {
    "Mathematics": "Natural sciences", "Physics and Astronomy": "Natural sciences",
    "Chemistry": "Natural sciences", "Earth and Planetary Sciences": "Natural sciences",
    "Environmental Science": "Natural sciences", "Computer Science": "Natural sciences",
    "Biochemistry, Genetics and Molecular Biology": "Natural sciences",
    "Immunology and Microbiology": "Natural sciences", "Neuroscience": "Natural sciences",
    "Engineering": "Engineering and technology",
    "Chemical Engineering": "Engineering and technology",
    "Materials Science": "Engineering and technology", "Energy": "Engineering and technology",
    "Medicine": "Medical and health sciences", "Nursing": "Medical and health sciences",
    "Dentistry": "Medical and health sciences", "Health Professions": "Medical and health sciences",
    "Pharmacology, Toxicology and Pharmaceutics": "Medical and health sciences",
    "Agricultural and Biological Sciences": "Agricultural and veterinary sciences",
    "Veterinary": "Agricultural and veterinary sciences",
    "Social Sciences": "Social sciences", "Psychology": "Social sciences",
    "Economics, Econometrics and Finance": "Social sciences",
    "Business, Management and Accounting": "Social sciences",
    "Decision Sciences": "Social sciences",
    "Arts and Humanities": "Humanities and the arts",
}


def short_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _all(client: Client, path: str, params: dict) -> list[dict]:
    out, page = [], 1
    while True:
        body = client.get_json(f"{API}/{path}", {**params, "per_page": 200, "page": page})
        results = (body or {}).get("results") or []
        out += results
        if len(results) < 200:
            return out
        page += 1


def build(client: Client, queries: int = QUERIES_PER_SUBFIELD) -> dict:
    fields = {short_id(f["id"]): f for f in _all(client, "fields", {})}
    subfields = sorted(_all(client, "subfields", {}), key=lambda s: int(short_id(s["id"])))
    areas = []
    for s in subfields:
        sid = short_id(s["id"])
        topics = (client.get_json(f"{API}/topics", {
            "filter": f"subfield.id:{sid}", "sort": "works_count:desc", "per_page": queries,
            "select": "id,display_name,keywords,works_count"}) or {}).get("results") or []
        field = fields[short_id(s["field"]["id"])]
        areas.append({
            "id": sid, "name": s["display_name"], "works_count": s.get("works_count"),
            "field": {"id": short_id(field["id"]), "name": field["display_name"]},
            "domain": {"id": short_id(s["domain"]["id"]), "name": s["domain"]["display_name"]},
            "frascati": FRASCATI.get(field["display_name"], "unmapped"),
            # every OpenAlex topic of the subfield, to tell which field a result belongs to
            "all_topics": sorted(t["display_name"] for t in s.get("topics") or []),
            "queries": [{"topic_id": short_id(t["id"]), "query": t["display_name"],
                         "keywords": [k["display_name"] if isinstance(k, dict) else k
                                      for k in (t.get("keywords") or [])[:6]],
                         "works_count": t.get("works_count")} for t in topics],
        })
    return {
        "source": "OpenAlex topic hierarchy (fields, subfields, topics)",
        "built": datetime.now(UTC).strftime("%Y-%m-%d"),
        "frascati_note": "Frascati groups are mapped per OpenAlex field by BAYAN; approximate.",
        "counts": {"domains": len({a["domain"]["id"] for a in areas}),
                   "fields": len({a["field"]["id"] for a in areas}), "subfields": len(areas)},
        "areas": areas,
    }


def save(taxonomy: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(taxonomy, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
