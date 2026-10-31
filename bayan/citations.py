"""BibTeX and RIS export of search results, for Zotero, Mendeley, EndNote, JabRef and others.

Only metadata the libraries supplied is written; nothing is filled in or guessed. A field the
record does not have is left out instead of being invented. Text stays UTF-8 (Zotero, Mendeley
and biblatex read it as is); characters that have a meaning in BibTeX are escaped.
"""

import re
import unicodedata

# The record's own type decides first, then its category.
_BIB_NATIVE = {
    "book-chapter": "incollection", "book-section": "incollection", "book-part": "incollection",
    "reference-entry": "incollection", "book": "book", "monograph": "book",
    "edited-book": "book", "reference-book": "book", "proceedings-article": "inproceedings",
    "conference-paper": "inproceedings", "dissertation": "phdthesis", "thesis": "phdthesis",
    "report": "techreport", "preprint": "misc", "posted-content": "misc", "dataset": "misc",
    "manual": "manual",
}
_BIB_CATEGORY = {"peer_reviewed": "article", "proceedings": "inproceedings",
                 "thesis": "phdthesis", "report": "techreport", "other": "misc"}
_VENUE_FIELD = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle",
                "phdthesis": "school", "techreport": "institution", "book": "publisher",
                "misc": "howpublished", "manual": "organization"}
_RIS_TYPE = {"article": "JOUR", "inproceedings": "CPAPER", "incollection": "CHAP",
             "book": "BOOK", "phdthesis": "THES", "techreport": "RPRT", "manual": "GEN",
             "misc": "GEN"}
_RIS_PREPRINT = {"preprint", "posted-content"}
_RIS_DATASET = {"dataset"}
# Braces become \textbraceleft{}/\textbraceright{}: BibTeX counts "\{" as a brace too, so one
# unbalanced brace in a title would otherwise swallow the rest of the file.
_SPECIAL = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "{": r"\textbraceleft{}", "}": r"\textbraceright{}", "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}"}
_STOP = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "with", "from", "at", "by"}


def entry_type(paper: dict) -> str:
    native = (paper.get("native_type") or "").lower()
    return _BIB_NATIVE.get(native) or _BIB_CATEGORY.get(paper.get("category") or "", "misc")


def bib_escape(text: object) -> str:
    """One line, with BibTeX/LaTeX special characters escaped (backslash first)."""
    text = " ".join(str(text or "").split())
    out = []
    for ch in text:
        if ch == "\\":
            out.append(r"\textbackslash{}")
        else:
            out.append(_SPECIAL.get(ch, ch))
    return "".join(out)


def _ascii_word(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def cite_key(paper: dict) -> str:
    """familyYEARword, ASCII only, e.g. smith2021neutron; "paper" when nothing is usable."""
    authors = paper.get("authors") or ([paper["first_author"]] if paper.get("first_author")
                                       else [])
    family = ""
    if authors:
        name = authors[0]
        family = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    words = [_ascii_word(w) for w in (paper.get("title") or "").split()]
    word = next((w for w in words if w and w not in _STOP), "")
    key = f"{_ascii_word(family)}{paper.get('year') or ''}{word}"
    return key if re.match(r"[a-z]", key) else f"paper{key}"


def unique_keys(papers: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    keys = []
    for p in papers:
        base = cite_key(p)
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append(base if n == 0 else f"{base}{_suffix(n)}")
    return keys


def _suffix(n: int) -> str:
    """1 -> a, 2 -> b, ... 26 -> z, 27 -> aa."""
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def _link(paper: dict) -> str:
    if paper.get("doi"):
        return f"https://doi.org/{paper['doi']}"
    return paper.get("landing_url") or paper.get("oa_url") or ""


def to_bibtex(papers: list[dict]) -> str:
    entries = ["% Exported from BAYAN. UTF-8. Fields come from the libraries' records;"
               " missing fields are left out, not guessed.\n"]
    for paper, key in zip(papers, unique_keys(papers), strict=True):
        kind = entry_type(paper)
        authors = paper.get("authors") or ([paper["first_author"]] if paper.get("first_author")
                                           else [])
        fields: list[tuple[str, str]] = [("title", paper.get("title") or "")]
        if authors:
            fields.append(("author", " and ".join(bib_escape(a) for a in authors)))
        if paper.get("year"):
            fields.append(("year", str(paper["year"])))
        if paper.get("venue"):
            fields.append((_VENUE_FIELD[kind], paper["venue"]))
        if paper.get("doi"):
            fields.append(("doi", paper["doi"]))
        if _link(paper):
            fields.append(("url", _link(paper)))
        if paper.get("language"):
            fields.append(("language", paper["language"]))
        if paper.get("keywords"):
            fields.append(("keywords", ", ".join(paper["keywords"])))
        if paper.get("abstract"):
            fields.append(("abstract", paper["abstract"]))
        fields.append(("note", f"BAYAN record {paper.get('source', '')}:{paper.get('id', '')}"))
        body = ",\n".join(f"  {name} = {{{value if name == 'author' else _field(name, value)}}}"
                          for name, value in fields)
        entries.append(f"@{kind}{{{key},\n{body}\n}}\n")
    return "\n".join(entries)


def _field(name: str, value: str) -> str:
    # URLs and DOIs keep their characters (biblatex and Zotero read them verbatim), except
    # braces, which would break the entry.
    if name in ("url", "doi"):
        return " ".join(value.split()).replace("{", "%7B").replace("}", "%7D")
    return bib_escape(value)


def to_ris(papers: list[dict]) -> str:
    lines: list[str] = []
    for paper in papers:
        kind = entry_type(paper)
        native = (paper.get("native_type") or "").lower()
        ty = ("INPR" if native in _RIS_PREPRINT else "DATA" if native in _RIS_DATASET
              else _RIS_TYPE[kind])
        tag = lambda t, v: lines.append(f"{t}  - {' '.join(str(v).split())}")  # noqa: E731
        tag("TY", ty)
        if paper.get("title"):
            tag("TI", paper["title"])
        for a in paper.get("authors") or ([paper["first_author"]] if paper.get("first_author")
                                          else []):
            tag("AU", a)
        if paper.get("year"):
            tag("PY", paper["year"])
        if paper.get("venue"):
            tag("JO" if kind == "article" else "T2" if kind in (
                "inproceedings", "incollection") else "PB", paper["venue"])
        if paper.get("doi"):
            tag("DO", paper["doi"])
        if _link(paper):
            tag("UR", _link(paper))
        if paper.get("language"):
            tag("LA", paper["language"])
        for k in paper.get("keywords") or []:
            tag("KW", k)
        if paper.get("abstract"):
            tag("AB", paper["abstract"])
        tag("N1", f"BAYAN record {paper.get('source', '')}:{paper.get('id', '')}")
        lines.append("ER  - ")
        lines.append("")
    return "\r\n".join(lines)
