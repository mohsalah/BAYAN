"""Evidence packets and prompts for research-gap and proposal requests.

No model is called here. The user submits the prompt to an assistant of their choice; a later
milestone can run the same request through the app's own agents.
"""

MAX_PAPERS = 40
ABSTRACT_CHARS = 2000
MAX_GAP_TEXT = 5000

GAP_RULES = """Rules:
- Use only the papers listed below. Cite them as [P1], [P2]... Do not invent papers, citations, results or data.
- The evidence is limited to titles, metadata and abstracts unless a paper says otherwise. Say so where it matters.
- Label every gap "unverified candidate". Never infer a gap only from counts or from missing metadata.
- For each gap give: (1) the gap in one or two sentences; (2) supporting papers; (3) what those papers do not address; (4) counterevidence that would disprove it and where to look; (5) broader literature searches to run (queries, databases, years); (6) a validation plan; (7) confidence (low/medium/high) with the reason.
- If the evidence supports fewer gaps than requested, return fewer and explain why."""

PROPOSAL_QUESTIONS = 3

PROPOSAL_SECTIONS = ("source-linked gap", "research question or hypothesis", "objectives",
                     "system, material or regime studied", "methods, tools and data",
                     "comparators and baselines", "validation", "units and uncertainty handling",
                     "reproducibility", "resources and timeline", "risks", "deliverables",
                     "success criteria")


def paper_block(index: int, paper: dict) -> str:
    abstract = (paper.get("abstract") or "").strip()
    if abstract:
        text = abstract[:ABSTRACT_CHARS] + ("..." if len(abstract) > ABSTRACT_CHARS else "")
        evidence = "abstract"
    else:
        text, evidence = "not supplied by the source (title and metadata only)", "metadata only"
    bits = [paper.get("first_author") or "", str(paper.get("year") or ""), paper.get("venue") or ""]
    return (
        f"[P{index}] {paper.get('title') or '(untitled)'}\n"
        f"    {' | '.join(b for b in bits if b)}\n"
        f"    DOI: {paper.get('doi') or 'none'} | type: {paper.get('category_label', '')} "
        f"({paper.get('native_type') or 'unknown'}) | language: {paper.get('language') or 'unknown'}\n"
        f"    Evidence level: {evidence}\n"
        f"    Abstract: {text}"
    )


def build_packet(kind: str, papers: list[dict], request: dict, selected_total: int) -> dict:
    included = papers[:MAX_PAPERS]
    with_abstract = sum(1 for p in included if (p.get("abstract") or "").strip())
    items = []
    for i, p in enumerate(included, start=1):
        items.append({
            "ref": f"P{i}", "source": p["source"], "id": p["id"], "doi": p.get("doi") or "",
            "title": p.get("title") or "", "first_author": p.get("first_author") or "",
            "year": p.get("year"), "venue": p.get("venue") or "",
            "reference_type": p.get("category_label", ""), "native_type": p.get("native_type") or "",
            "language": p.get("language") or "", "countries": p.get("countries") or [],
            "evidence_level": "abstract" if (p.get("abstract") or "").strip() else "metadata_only",
            "abstract": (p.get("abstract") or "")[:ABSTRACT_CHARS],
        })
    return {
        "kind": kind, "request": request, "papers_selected": selected_total,
        "papers_included": len(included), "papers_with_abstract": with_abstract,
        "truncated": selected_total > len(included),
        "evidence_note": "Abstract-level evidence only; no full texts were read.",
        "papers": items,
    }


def _coverage(packet: dict) -> str:
    line = (f"Papers selected: {packet['papers_selected']}; included here: "
            f"{packet['papers_included']}; with an abstract: {packet['papers_with_abstract']}.")
    if packet["truncated"]:
        line += f" Only the first {MAX_PAPERS} are included."
    return line


def gap_prompt(packet: dict, papers: list[dict], gap_count: int, question: str) -> str:
    plural = "s" if gap_count != 1 else ""
    area = question.strip() or "the research area covered by the papers below"
    blocks = "\n\n".join(paper_block(i, p) for i, p in enumerate(papers[:MAX_PAPERS], start=1))
    return (
        f"Task: propose up to {gap_count} candidate research gap{plural} for: {area}\n\n"
        f"{GAP_RULES}\n\n{_coverage(packet)}\n\nPapers:\n\n{blocks}\n"
    )


def proposal_prompt(packet: dict, papers: list[dict], count: int, gap_text: str,
                    question: str) -> str:
    plural = "s" if count != 1 else ""
    area = question.strip() or "the research area covered by the papers below"
    blocks = "\n\n".join(paper_block(i, p) for i, p in enumerate(papers[:MAX_PAPERS], start=1))
    sections = "; ".join(PROPOSAL_SECTIONS)
    return (
        f"Task: draft {count} distinct research proposal{plural} for: {area}\n\n"
        f"Research gap(s) to develop (written by the researcher, still unverified):\n"
        f"{gap_text.strip()}\n\n"
        "Rules:\n"
        "- Ground each proposal in the gap(s) above and in the papers listed below; cite them as [P1], [P2]...\n"
        "- Do not invent novelty claims, results, citations, costs or facility access. Mark anything unknown as an assumption.\n"
        "- The proposals must differ in question or method, not only in wording.\n"
        f"- Give exactly {PROPOSAL_QUESTIONS} suggested research questions per proposal. Each must be answerable "
        "with the stated methods and data, tied to the gap and to cited papers, and different from the others.\n"
        f"- Each proposal must cover: {sections}.\n"
        "- State what must still be verified (counterevidence, wider literature) before the work starts.\n\n"
        f"{_coverage(packet)}\n\nPapers:\n\n{blocks}\n"
    )
