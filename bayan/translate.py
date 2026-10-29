"""M24: translator (any of the six languages -> English or Arabic) behind a swappable engine.

Rules this module enforces (see AGENTS.md):
- Output language is English or Arabic only.
- Numbers, units, equations, nuclide/chemical names, DOIs, URLs and citations are swapped for
  placeholders before the engine sees the text and restored afterwards.
- Every result is labelled machine translated, with source language, target language and engine.
  A translation is never an evidence quote and never replaces the original text.
- No engine configured -> `NotConfigured`; a translation is never faked.
"""

import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from .models import LANGUAGES

TARGETS = ("en", "ar")
MACHINE_LABEL = "machine translated"
MAX_GLOSSARY_TERMS = 500
MAX_TERM_LENGTH = 100
SEGMENT_LIMIT = 1500  # characters per engine call
ENGINE_TIMEOUT = 60.0
NOT_CONFIGURED_MESSAGE = (
    "Translator not configured. Set BAYAN_TRANSLATE_URL to a LibreTranslate-compatible server "
    "(self-hosted LibreTranslate is free and needs no paid key)."
)


class TranslateError(Exception):
    """A request the translator cannot honour; the message is safe to show to the user."""


class NotConfigured(TranslateError):
    pass


class EngineError(TranslateError):
    pass


class Engine(Protocol):
    """A translation engine. Swap it by passing another object with this shape."""

    name: str

    def translate(self, text: str, source: str, target: str) -> str: ...


class LibreTranslateEngine:
    """LibreTranslate-compatible HTTP engine. Free when self-hosted; the API key is optional."""

    name = "libretranslate"

    def __init__(self, url: str, api_key: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=ENGINE_TIMEOUT)

    def translate(self, text: str, source: str, target: str) -> str:
        body = {"q": text, "source": source, "target": target, "format": "text"}
        if self._api_key:
            body["api_key"] = self._api_key
        try:
            resp = self._client.post(f"{self._url}/translate", json=body)
        except httpx.HTTPError as exc:
            # Only the exception type is reported: the message could carry the request URL/body.
            raise EngineError(f"Translation engine unreachable ({type(exc).__name__}).") from exc
        if resp.status_code != 200:
            raise EngineError(f"Translation engine returned HTTP {resp.status_code}.")
        try:
            out = resp.json()["translatedText"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EngineError("Translation engine returned an unexpected response.") from exc
        if not isinstance(out, str):
            raise EngineError("Translation engine returned an unexpected response.")
        return out


def engine_from_settings(url: str | None, api_key: str | None) -> Engine | None:
    return LibreTranslateEngine(url, api_key) if url else None


# ------------------------------------------------------------------ language handling

def check_pair(source: str, target: str) -> None:
    if target not in TARGETS:
        raise TranslateError("The translator only produces English (en) or Arabic (ar).")
    if source not in LANGUAGES:
        raise TranslateError("Source language must be one of en, ar, fr, zh, de, es.")
    if source == target:
        raise TranslateError("The text is already in the target language.")


_STOPWORDS = {
    "en": {"the", "and", "of", "is", "are", "with", "that", "for", "this", "from", "was"},
    "fr": {"le", "la", "les", "des", "est", "une", "dans", "pour", "que", "avec", "sur", "du"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "den", "von", "ein", "eine", "auf"},
    "es": {"el", "los", "las", "una", "es", "con", "para", "que", "del", "por", "en", "se"},
}


def detect_language(text: str) -> str | None:
    """Best-effort guess among the six languages; None when unsure (the user must choose)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    if sum("؀" <= c <= "ۿ" for c in letters) > len(letters) * 0.3:
        return "ar"
    if sum("一" <= c <= "鿿" for c in letters) > len(letters) * 0.3:
        return "zh"
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    scores = {lang: sum(w in sw for w in words) for lang, sw in _STOPWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] == 0 or ranked[0] == ranked[1]:
        return None
    return best


# ------------------------------------------------------------------ protection

_ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As "
    "Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd "
    "Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am "
    "Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()
_SYM = "|".join(sorted(_ELEMENTS, key=len, reverse=True))

_UNIT = (
    r"(?:°\s?[CFK]|℃|%|‰|"
    r"(?:[kMGTPmµμnpf]?(?:eV|Hz|Pa|Bq|Gy|Sv|Ci|mol|cal|bar|barn|[WJVATgmsKLNΩ])"
    r"(?:\^?[−-]?\d+|[²³⁻¹]+)?(?:/[kMGTmµμnp]?[A-Za-z]+(?:\^?\d+|[²³]+)?)*)|"
    r"pcm|ppm|ppb|rpm|dB)"
)

_PATTERNS = [
    ("eq", r"\$\$.+?\$\$|\$[^$\n]+\$|\\\(.+?\\\)|\\\[.+?\\\]"),
    ("doi", r"\b10\.\d{4,9}/[^\s\"<>]*[^\s\"<>.,;:)\]]"),
    ("url", r"https?://[^\s\"<>]*[^\s\"<>.,;:)\]]"),
    ("cite", r"\[\d+(?:\s*[,–-]\s*\d+)*\]|"
             r"\([A-ZÀ-Ý][^()\n]{0,60}?(?:1[5-9]|20)\d{2}[a-z]?"
             r"(?:\s*[;,][^()\n]{0,60}?(?:1[5-9]|20)\d{2}[a-z]?)*\)"),
    ("nuc", rf"\b(?:{_SYM})-\d{{1,3}}m?\b|\b\d{{1,3}}m?(?:{_SYM})\b|[⁰-⁹]+(?:{_SYM})\b"),
    ("chem", rf"\b(?:(?:{_SYM})\d*)+\b"),
    ("eqs", r"(?<!\w)[A-Za-z][A-Za-z0-9_{}\\^]*(?:\([^)\n]*\))?\s*(?:=|≈|≤|≥|<|>|∼)\s*"
            r"[-+±−]?[\w.^{}\\()*/+\-]+(?:\s*±\s*[\d.]+)?"),
    ("num", rf"(?<![\w.])[±+−-]?\d+(?:[.,]\d+)*(?:[eE][+\-−]?\d+)?(?:\s?{_UNIT}(?![A-Za-z°]))?"),
    ("sym", r"[∑∫∂∇∞≈≤≥≠±√×÷∝∈∉⊂⊃∪∩∘Α-Ωα-ω]+"),
]
_TOKEN = re.compile(r"XQ\s*(\d+)\s*X")


def _token(index: int) -> str:
    return f"XQ{index}X"


def _valid_chem(text: str) -> bool:
    symbols = re.findall(rf"(?:{_SYM})\d*", text)
    return any(c.isdigit() for c in text) or (len(symbols) >= 2 and any(c.islower() for c in text))


@dataclass
class Protected:
    text: str
    originals: list[str]
    glossary_used: dict[str, str] = field(default_factory=dict)


def _glossary_pattern(terms: list[str]) -> str:
    alts = []
    for term in sorted(terms, key=len, reverse=True):
        esc = re.escape(term)
        alts.append(rf"(?<!\w){esc}(?!\w)" if re.match(r"\w", term[0]) and re.match(r"\w", term[-1])
                    else esc)
    return "(?i:" + "|".join(alts) + ")"


def protect(text: str, glossary: dict[str, str] | None = None) -> Protected:
    """Replace protected spans and glossary terms with placeholders in one left-to-right pass.

    `glossary` maps source term -> target term (matched case-insensitively).
    """
    lookup = {k.lower(): v for k, v in (glossary or {}).items() if k}
    parts = [("lit", r"XQ\s*\d+\s*X")]  # stray placeholder-like text in the source stays itself
    if lookup:
        parts.append(("gl", _glossary_pattern(list(lookup))))
    parts += _PATTERNS
    master = re.compile("|".join(f"(?P<{n}>{p})" for n, p in parts), re.DOTALL)
    originals: list[str] = []
    used: dict[str, str] = {}

    def swap(m: re.Match) -> str:
        kind, raw = m.lastgroup, m.group()
        if kind == "chem" and not _valid_chem(raw):
            return raw
        if kind == "gl":
            target = lookup[raw.lower()]
            used[raw.lower()] = target
            originals.append(target)
        else:
            originals.append(raw)
        return _token(len(originals) - 1)

    return Protected(master.sub(swap, text), originals, used)


def restore(text: str, originals: list[str]) -> tuple[str, int]:
    """Put the originals back; returns the text and how many placeholders the engine lost."""
    seen: set[int] = set()

    def back(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx >= len(originals):
            return m.group()
        seen.add(idx)
        return originals[idx]

    out = _TOKEN.sub(back, text)
    return out, len(originals) - len(seen)


# ------------------------------------------------------------------ translation

def split_segments(text: str, limit: int = SEGMENT_LIMIT) -> list[str]:
    """Paragraph-preserving pieces no longer than `limit`; blank lines are kept as ''."""
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= limit:
            out.append(line)
            continue
        current = ""
        for sentence in re.split(r"(?<=[.!?。！？؟])\s*", line):
            while len(sentence) > limit:
                if current:
                    out.append(current)
                    current = ""
                out.append(sentence[:limit])
                sentence = sentence[limit:]
            if current and len(current) + len(sentence) + 1 > limit:
                out.append(current)
                current = ""
            current = f"{current} {sentence}".strip()
        if current:
            out.append(current)
    return out


@dataclass
class Translation:
    source_language: str
    target_language: str
    engine: str
    segments: list[dict]
    warnings: list[str]
    glossary_applied: dict[str, str]
    origin: str = "text"  # text | pdf_text | ocr

    @property
    def text(self) -> str:
        return "\n".join(s["translation"] for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "label": MACHINE_LABEL,
            "machine_translated": True,
            "evidence_quote": False,  # translations are never evidence quotes
            "source_language": self.source_language,
            "target_language": self.target_language,
            "engine": self.engine,
            "origin": self.origin,
            "glossary_applied": self.glossary_applied,
            "warnings": self.warnings,
            "segments": self.segments,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Translation":
        return cls(data["source_language"], data["target_language"], data["engine"],
                   data["segments"], data["warnings"], data["glossary_applied"],
                   data.get("origin", "text"))


def translate_text(engine: Engine | None, text: str, source: str, target: str,
                   glossary: dict[str, str] | None = None, origin: str = "text") -> Translation:
    if engine is None:
        raise NotConfigured(NOT_CONFIGURED_MESSAGE)
    check_pair(source, target)
    segments: list[dict] = []
    warnings: list[str] = []
    applied: dict[str, str] = {}
    for original in split_segments(text):
        if not original.strip():
            segments.append({"original": original, "translation": original})
            continue
        protected = protect(original, glossary)
        applied.update(protected.glossary_used)
        raw = engine.translate(protected.text, source, target)
        translated, lost = restore(raw, protected.originals)
        if lost:
            warnings.append(
                f"Low confidence: {lost} protected item(s) (number, unit, equation, name or "
                f"glossary term) were lost by the engine in: {original[:60]!r}")
        if not translated.strip():
            warnings.append(f"Low confidence: the engine returned nothing for: {original[:60]!r}")
        segments.append({"original": original, "translation": translated})
    return Translation(source, target, engine.name, segments, warnings, applied, origin)


# ------------------------------------------------------------------ glossary (per user)

def clean_term(value: object) -> str:
    term = " ".join(str(value or "").split())
    if not term or len(term) > MAX_TERM_LENGTH:
        raise TranslateError(f"A glossary term must be 1-{MAX_TERM_LENGTH} characters.")
    return term


def save_glossary_term(conn: sqlite3.Connection, user_id: int, source: str, target: str,
                       source_term: str, target_term: str) -> None:
    check_pair(source, target)
    source_term, target_term = clean_term(source_term), clean_term(target_term)
    key = source_term.lower()
    exists = conn.execute(
        "SELECT 1 FROM glossary WHERE user_id = ? AND source_language = ? AND target_language = ?"
        " AND term_key = ?", (user_id, source, target, key)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM glossary WHERE user_id = ?",
                         (user_id,)).fetchone()[0]
    if not exists and count >= MAX_GLOSSARY_TERMS:
        raise TranslateError(f"A glossary holds at most {MAX_GLOSSARY_TERMS} terms.")
    conn.execute(
        "INSERT INTO glossary(user_id, source_language, target_language, term_key, source_term,"
        " target_term) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, source_language, target_language,"
        " term_key) DO UPDATE SET source_term = excluded.source_term,"
        " target_term = excluded.target_term",
        (user_id, source, target, key, source_term, target_term))
    conn.commit()


def delete_glossary_term(conn: sqlite3.Connection, user_id: int, source: str, target: str,
                         source_term: str) -> bool:
    cur = conn.execute(
        "DELETE FROM glossary WHERE user_id = ? AND source_language = ? AND target_language = ?"
        " AND term_key = ?", (user_id, source, target, " ".join(source_term.split()).lower()))
    conn.commit()
    return cur.rowcount > 0


def list_glossary(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT source_language, target_language, source_term, target_term FROM glossary"
        " WHERE user_id = ? ORDER BY source_language, target_language, term_key",
        (user_id,)).fetchall()
    return [dict(r) for r in rows]


def glossary_for(conn: sqlite3.Connection, user_id: int, source: str,
                 target: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT source_term, target_term FROM glossary WHERE user_id = ? AND source_language = ?"
        " AND target_language = ?", (user_id, source, target)).fetchall()
    return {r["source_term"]: r["target_term"] for r in rows}


def glossary_version(glossary: dict[str, str]) -> str:
    blob = json.dumps(sorted((k.lower(), v) for k, v in glossary.items()), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ cache and rate limit

def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cached_or_translate(conn: sqlite3.Connection, user_id: int, engine: Engine | None,
                        text: str, source: str, target: str, glossary: dict[str, str],
                        origin: str = "text") -> tuple[Translation, bool]:
    """Translate with a per-user cache keyed by text hash, languages, engine and glossary."""
    if engine is None:
        raise NotConfigured(NOT_CONFIGURED_MESSAGE)
    check_pair(source, target)
    key = (user_id, _text_hash(text), source, target, engine.name, glossary_version(glossary),
           origin)
    row = conn.execute(
        "SELECT result FROM translation_cache WHERE user_id = ? AND text_hash = ? AND"
        " source_language = ? AND target_language = ? AND engine = ? AND glossary_version = ?"
        " AND origin = ?", key).fetchone()
    if row is not None:
        return Translation.from_dict(json.loads(row["result"])), True
    result = translate_text(engine, text, source, target, glossary, origin)
    if not result.warnings:  # never cache a low-confidence result
        conn.execute(
            "INSERT OR REPLACE INTO translation_cache(user_id, text_hash, source_language,"
            " target_language, engine, glossary_version, origin, result) VALUES(?,?,?,?,?,?,?,?)",
            (*key, json.dumps(result.to_dict(), ensure_ascii=False)))
        conn.commit()
    return result, False


def charge(conn: sqlite3.Connection, user_id: int, chars: int, per_hour: int,
           max_chars: int) -> None:
    """Enforce the per-request size limit and the per-user hourly rate; then record the use."""
    if chars > max_chars:
        raise TranslateError(f"Text is too long: {chars} characters (limit {max_chars}).")
    recent = conn.execute(
        "SELECT COUNT(*) FROM translate_usage WHERE user_id = ?"
        " AND created_at > datetime('now', '-1 hour')", (user_id,)).fetchone()[0]
    if recent >= per_hour:
        raise RateLimited(f"Translation limit reached ({per_hour} requests per hour).")
    conn.execute("INSERT INTO translate_usage(user_id, chars) VALUES(?,?)", (user_id, chars))
    conn.commit()


class RateLimited(TranslateError):
    pass


# ------------------------------------------------------------------ PDF text and OCR

OCR_LANGS = {"en": "eng", "ar": "ara", "fr": "fra", "zh": "chi_sim", "de": "deu", "es": "spa"}
SCANNED_MIN_CHARS = 20  # fewer extractable characters on a page means it is probably an image
OCR_MISSING_MESSAGE = (
    "This PDF looks scanned (no selectable text) and OCR is not installed. Install the optional "
    "OCR extra (pip install 'bayan[ocr]') plus the Tesseract program, or upload a text PDF."
)


def ocr_available() -> bool:
    return (importlib.util.find_spec("pytesseract") is not None
            and importlib.util.find_spec("pdf2image") is not None
            and shutil.which("tesseract") is not None)


def ocr_page(path: Path, page: int, language: str) -> str:
    """OCR one 1-based page. Only called when `ocr_available()`; imports stay lazy."""
    from pdf2image import convert_from_path
    from pytesseract import image_to_string

    images = convert_from_path(str(path), first_page=page, last_page=page, dpi=200)
    return image_to_string(images[0], lang=OCR_LANGS.get(language, "eng")) if images else ""


def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,5' -> [1,2,3,5], 1-based, within the document; None -> every page."""
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise TranslateError(f"Invalid page selection: {part!r}")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        if first < 1 or last < first or last > total:
            raise TranslateError(f"Pages must be within 1-{total}.")
        pages.extend(p for p in range(first, last + 1) if p not in pages)
    return pages
