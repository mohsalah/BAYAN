"""Gold set: N real papers per subfield over a period, each verified, with its provenance.

Selection per subfield (proportions of N, 50 -> 15/25/10):
- landmark (30%): most cited in the period;
- mid (50%): a seeded random sample of papers cited at least MID_MIN_CITES times, spread over
  five equal spans of the period (before the recent window);
- recent (20%): most cited of the last RECENT_YEARS years of the period.
Papers come from OpenAlex (primary topic in the subfield; retracted works, paratext, errata,
editorials, letters, abstracts-only and similar are excluded). Records with a DOI are preferred;
a stratum is filled with DOI-less records only when DOIs run out (common in the humanities),
and those are marked id_type "openalex".

Verification: a DOI must exist in Crossref with a matching title, or be registered at doi.org
(DataCite and other agencies). A record that fails is dropped and counted, never kept.
Abstracts are not stored.
"""

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from .client import Client, FetchError
from .taxonomy import API, short_id

EXCLUDED_TYPES = ("paratext", "erratum", "retraction", "editorial", "letter", "peer-review",
                  "libguides", "grant", "supplementary-materials", "conference-abstract",
                  "reference-entry", "software", "dataset", "standard", "other")
SELECT = ("id,doi,display_name,publication_year,type,language,primary_location,authorships,"
          "cited_by_count,primary_topic,open_access,ids,locations")
# Without a DOI, the identifier each field normally uses, best first. Each is checked at the
# registry that issues it, except library catalogue numbers, which are taken from OpenAlex's
# location records and labelled so.
ID_LADDER = ("pmid", "pmcid", "arxiv", "isbn", "handle", "lccn", "ncid")
BOOK_TYPES = {"book", "book-chapter", "monograph", "edited-book"}
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?$", re.I)
_HANDLE = re.compile(r"(?:hdl\.handle\.net/|/handle/)(\d+(?:\.\d+)*/[^\s?#/]+)", re.I)
_LCCN = re.compile(r"loc\.gov/catdir/[^\s]*?/(\d{8,10})\.html", re.I)
_NCID = re.compile(r"ci\.nii\.ac\.jp/ncid/([A-Z]{2}\d{5,})", re.I)
_ISBN = re.compile(r"(?<!\d)(97[89]\d{10})(?!\d)")
MID_MIN_CITES = 5
RECENT_YEARS = 3
MID_SPANS = 5
TITLE_MATCH = 0.8
MAX_AUTHORS = 20
# DOI prefixes that OpenAlex lists as articles but that are reviews of another work, e.g.
# Choice Reviews Online (10.5860/choice.*): the review, not the book, would become the gold paper.
REVIEW_RECORD_PREFIXES = ("10.5860/choice",)


def norm_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def title_score(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    if a == b or (len(a) > 20 and (a in b or b in a)):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def strata(n: int) -> dict[str, int]:
    landmark, recent = round(n * 0.3), round(n * 0.2)
    return {"landmark": landmark, "mid": n - landmark - recent, "recent": recent}


def spans(year_from: int, year_to: int, parts: int = MID_SPANS) -> list[tuple[int, int]]:
    """Equal spans of [year_from, year_to]: at least one year each, fewer for short periods."""
    years = list(range(year_from, year_to + 1))
    if not years:
        return []
    parts = min(parts, len(years))
    size, extra = divmod(len(years), parts)
    out, i = [], 0
    for k in range(parts):
        width = size + (1 if k < extra else 0)
        out.append((years[i], years[i + width - 1]))
        i += width
    return out


def _filter(sid: str, y0: int, y1: int, extra: str = "") -> str:
    parts = [f"primary_topic.subfield.id:{sid}", f"publication_year:{y0}-{y1}",
             "is_retracted:false", *(f"type:!{t}" for t in EXCLUDED_TYPES)]
    return ",".join(parts + ([extra] if extra else []))


def candidates(client: Client, sid: str, y0: int, y1: int, want: int, *, sort: str | None = None,
               min_cites: int = 0, seed: int | None = None) -> list[dict]:
    """Up to 2 x `want` works, DOI-bearing ones first."""
    out: list[dict] = []
    for doi_flag in ("has_doi:true", "has_doi:false"):
        extra = doi_flag + (f",cited_by_count:>{min_cites - 1}" if min_cites else "")
        params = {"filter": _filter(sid, y0, y1, extra), "per_page": min(200, want * 2),
                  "select": SELECT}
        if sort:
            params["sort"] = sort
        if seed is not None:
            params.update(sample=min(200, want * 2), seed=seed)
        out += (client.get_json(f"{API}/works", params) or {}).get("results") or []
        if len(out) >= want * 2:
            break
    return out


def isbn13_ok(isbn: str) -> bool:
    if not re.fullmatch(r"97[89]\d{10}", isbn):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(isbn[:12]))
    return (10 - total % 10) % 10 == int(isbn[12])


def identifiers(work: dict) -> dict[str, str]:
    """Identifiers OpenAlex lists for the work: its ids and those in its location links."""
    ids = work.get("ids") or {}
    out: dict[str, str] = {}
    if ids.get("pmid"):
        out["pmid"] = str(ids["pmid"]).rstrip("/").rsplit("/", 1)[-1]
    if ids.get("pmcid"):
        pmc = str(ids["pmcid"]).rstrip("/").rsplit("/", 1)[-1].upper()
        out["pmcid"] = pmc if pmc.startswith("PMC") else f"PMC{pmc}"
    urls = [loc.get(k) or "" for loc in work.get("locations") or []
            for k in ("landing_page_url", "pdf_url")]
    for name, rx in (("arxiv", _ARXIV), ("handle", _HANDLE), ("lccn", _LCCN), ("ncid", _NCID)):
        hit = next((m.group(1) for u in urls if (m := rx.search(u))), None)
        if hit:
            out[name] = hit
    isbn = next((m.group(1) for u in urls for m in _ISBN.finditer(u) if isbn13_ok(m.group(1))),
                None)
    if isbn:
        out["isbn"] = isbn
    return out


def _family(name: str) -> str:
    part = (name or "").split(",")[0] if "," in (name or "") else ((name or "").split() or [""])[-1]
    return re.sub(r"[\W_]+", "", part.casefold())


def check_identifier(client: Client, work: dict, kind: str, value: str) -> tuple[bool, str]:
    """(confirmed, detail) for a non-DOI identifier, at the registry that issues it."""
    title = work.get("display_name") or ""
    if kind in ("pmid", "pmcid"):
        query = f"EXT_ID:{value} AND SRC:MED" if kind == "pmid" else f"PMCID:{value}"
        body = client.get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                               {"query": query, "format": "json", "resultType": "lite"})
        hits = ((body or {}).get("resultList") or {}).get("result") or []
        score = max([title_score(title, h.get("title") or "") for h in hits] or [0.0])
        return score >= TITLE_MATCH, f"Europe PMC title match {score:.2f}"
    if kind == "arxiv":
        xml = client.get_text("https://export.arxiv.org/api/query", {"id_list": value}) or ""
        found = re.findall(r"<entry>.*?<title>(.*?)</title>", xml, re.S)
        score = max([title_score(title, t) for t in found] or [0.0])
        return score >= TITLE_MATCH, f"arXiv title match {score:.2f}"
    if kind == "isbn":
        body = client.get_json(f"https://openlibrary.org/isbn/{value}.json")
        score = title_score(title, (body or {}).get("title") or "") if body else 0.0
        return score >= TITLE_MATCH, f"Open Library title match {score:.2f}"
    if kind == "handle":
        body = client.get_json(f"https://hdl.handle.net/api/handles/{value}")
        return bool(body and body.get("responseCode") == 1), "registered at hdl.handle.net"
    # catalogue numbers come from OpenAlex's own location record; they are not re-checked
    return True, "library catalogue number from the OpenAlex location record (not re-checked)"


def find_isbn(client: Client, work: dict) -> tuple[str, str] | None:
    """A book's ISBN-13 from Open Library, only on a title, author and year match."""
    authors = [a["author"]["display_name"] for a in work.get("authorships") or []
               if (a.get("author") or {}).get("display_name")]
    params = {"title": work.get("display_name") or "", "limit": 5,
              "fields": "title,author_name,first_publish_year,isbn"}
    if authors:
        params["author"] = _family(authors[0])
    docs = (client.get_json("https://openlibrary.org/search.json", params) or {}).get("docs") or []
    for d in docs:
        score = title_score(work.get("display_name") or "", d.get("title") or "")
        fam = {_family(a) for a in d.get("author_name") or []}
        year = d.get("first_publish_year")
        if score < 0.9 or (authors and _family(authors[0]) not in fam):
            continue
        if year and work.get("publication_year") and abs(year - work["publication_year"]) > 2:
            continue
        isbn = next((i for i in d.get("isbn") or [] if isbn13_ok(i)), None)
        if isbn:
            return isbn, f"Open Library title {score:.2f} + author + year match"
    return None


def verify(client: Client, work: dict) -> tuple[str, str]:
    """(status, detail). Status starts with "ok_" when the record may be used."""
    title = work.get("display_name") or ""
    if not title.strip() or not work.get("publication_year"):
        return "drop_incomplete", "no title or no year"
    doi = (work.get("doi") or "").lower().removeprefix("https://doi.org/")
    if not doi:
        try:
            ids = identifiers(work)
            for kind in ID_LADDER:
                if kind in ids:
                    ok, detail = check_identifier(client, work, kind, ids[kind])
                    if ok:
                        work["_id"] = (kind, ids[kind])
                        return f"ok_{kind}", detail
            if (work.get("type") or "") in BOOK_TYPES:
                hit = find_isbn(client, work)
                if hit:
                    work["_id"] = ("isbn", hit[0])
                    return "ok_isbn", hit[1]
        except FetchError as exc:
            return "drop_check_failed", str(exc)[:200]
        return "ok_openalex_only", "no DOI and no other checked identifier; OpenAlex record"
    if doi.startswith(REVIEW_RECORD_PREFIXES):
        return "drop_review_record", "a review of another work (Choice Reviews), not the work"
    try:
        body = client.get_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
        if body and body.get("message"):
            msg = body["message"]
            score = max([title_score(title, t) for t in msg.get("title") or [""]] or [0.0])
            if score >= TITLE_MATCH:
                return "ok_crossref", f"title match {score:.2f}"
            return "drop_crossref_title_mismatch", f"title match {score:.2f}"
        handle = client.get_json(f"https://doi.org/api/handles/{quote(doi, safe='/')}")
        if handle and handle.get("responseCode") == 1:
            return "ok_doi_registered", "registered at doi.org (not a Crossref DOI)"
        return "drop_doi_not_found", "DOI not found at Crossref or doi.org"
    except FetchError as exc:
        return "drop_check_failed", str(exc)[:200]


def record(work: dict, area: dict, stratum: str, status: str, detail: str,
           checked: str) -> dict:
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    topic = work.get("primary_topic") or {}
    doi = (work.get("doi") or "").lower().removeprefix("https://doi.org/")
    authors = [a["author"]["display_name"] for a in work.get("authorships") or []
               if (a.get("author") or {}).get("display_name")]
    return {
        "openalex_id": short_id(work["id"]), "doi": doi,
        "id_type": "doi" if doi else (work.get("_id") or ("openalex",))[0],
        "id_value": doi or (work.get("_id") or ("", short_id(work["id"])))[1],
        "identifiers": {**identifiers(work),
                        **(dict([work["_id"]]) if work.get("_id") else {})},
        "title": work.get("display_name") or "", "authors": authors[:MAX_AUTHORS],
        "first_author": authors[0] if authors else "", "year": work.get("publication_year"),
        "venue": src.get("display_name") or "", "type": work.get("type") or "",
        "language": work.get("language") or "",
        "subfield_id": area["id"], "field_id": area["field"]["id"],
        "domain_id": area["domain"]["id"], "stratum": stratum,
        "oa_url": (work.get("open_access") or {}).get("oa_url") or "",
        "citation_count": work.get("cited_by_count") or 0,
        "topic_id": short_id(topic["id"]) if topic.get("id") else "",
        "query": topic.get("display_name") or "",
        "provenance": {"source": "api.openalex.org/works", "check": status, "detail": detail,
                       "checked": checked},
    }


def build_area(client: Client, area: dict, year_from: int, year_to: int, n: int,
               seed: int = 1) -> tuple[list[dict], dict]:
    sid, want = area["id"], strata(n)
    recent_from = max(year_from, year_to - RECENT_YEARS + 1)
    mid_to = recent_from - 1 if recent_from > year_from else year_to
    plan: list[tuple[str, int, list[dict]]] = []
    plan.append(("landmark", want["landmark"], candidates(
        client, sid, year_from, year_to, want["landmark"], sort="cited_by_count:desc")))
    mid_spans = spans(year_from, mid_to)
    for k, (a, b) in enumerate(mid_spans):
        share = want["mid"] // len(mid_spans) + (1 if k < want["mid"] % len(mid_spans) else 0)
        plan.append(("mid", share, candidates(client, sid, a, b, share,
                                              min_cites=MID_MIN_CITES, seed=seed + k)))
    plan.append(("recent", want["recent"], candidates(
        client, sid, recent_from, year_to, want["recent"], sort="cited_by_count:desc")))
    chosen: list[dict] = []
    seen: set[str] = set()
    checks: dict[str, int] = {}
    short: dict[str, int] = {}
    for stratum, need, works in plan:
        got = 0
        for work in works:
            if got >= need:
                break
            wid = short_id(work["id"])
            if wid in seen:
                continue
            seen.add(wid)
            status, detail = verify(client, work)
            checks[status] = checks.get(status, 0) + 1
            if status.startswith("ok_"):
                chosen.append(record(work, area, stratum, status, detail,
                                     datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
                got += 1
        if got < need:
            short[stratum] = short.get(stratum, 0) + need - got
    summary = {"subfield_id": sid, "name": area["name"], "wanted": n, "kept": len(chosen),
               "by_stratum": {s: sum(1 for r in chosen if r["stratum"] == s) for s in want},
               "shortfall": short, "checks": checks,
               "without_doi": sum(1 for r in chosen if not r["doi"])}
    return chosen, summary


def build(client: Client, taxonomy: dict, out_dir: Path, year_from: int, year_to: int, n: int,
          only: list[str] | None = None, workers: int = 4, log=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    areas = [a for a in taxonomy["areas"] if not only or a["id"] in only]

    def one(area: dict) -> dict:
        path = out_dir / f"{area['id']}.jsonl"
        try:
            rows, summary = build_area(client, area, year_from, year_to, n)
        except FetchError as exc:
            log(f"{area['id']} {area['name']}: failed ({exc})")
            return {"subfield_id": area["id"], "name": area["name"], "error": str(exc)[:200]}
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        log(f"{area['id']} {area['name']}: {summary['kept']}/{n}")
        return summary

    with ThreadPoolExecutor(max_workers=workers) as pool:
        summaries = list(pool.map(one, areas))
    meta = {"period": [year_from, year_to], "per_area": n, "areas": len(areas),
            "kept": sum(s.get("kept", 0) for s in summaries),
            "failed_areas": [s["subfield_id"] for s in summaries if "error" in s],
            "summaries": summaries}
    (out_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
    return meta


def load(gold_dir: Path, subfield_id: str) -> list[dict]:
    path = gold_dir / f"{subfield_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
