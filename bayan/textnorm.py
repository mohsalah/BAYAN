"""Text normalisation and tokenisation for the local full-text table (`paper_search`).

Matching only: the stored paper text is never changed. What is stored in the FTS table is
`index_text(text)`, and a user's words are turned into an FTS5 query by `fts_query(text)`,
so both sides go through the same steps.

- Latin scripts (fr, de, es, en): case-folded, accents and diacritics removed
  ("Élève" matches "eleve", "Über" matches "uber"; "ß" folds to "ss").
- Arabic: tashkeel (vowel marks) and tatweel removed, alef variants (أ إ آ ٱ) become ا.
  Other letter pairs (ى/ي, ة/ه) are left alone: merging them can join different words.
- Chinese: there is no word segmenter (no new dependency). Every run of CJK characters is
  split into overlapping character pairs (bigrams); a single isolated character stays as it
  is. A query is split the same way, so a phrase matches where its pairs occur together.
  Fallback and limit: bigrams find a phrase but also match across word boundaries, so
  precision is lower than with a real segmenter.
"""

import re
import unicodedata

# Tashkeel (U+064B-U+0652), superscript alef (U+0670), Quranic marks (U+06D6-U+06ED).
_ARABIC_MARKS = re.compile("[ً-ْٰۖ-ۭ]")
_TATWEEL = "ـ"
_ALEF_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا",
                                "آ": "ا", "ٱ": "ا"})
# CJK Unified Ideographs, Extension A, compatibility ideographs and Extension B-F.
_CJK = (r"㐀-䶿一-鿿豈-﫿"
        r"\U00020000-\U0002fa1f")
_TOKEN = re.compile(rf"[{_CJK}]+|[^\W_{_CJK}]+")
_IS_CJK = re.compile(rf"[{_CJK}]")


def normalize_arabic(text: str) -> str:
    """Strip tashkeel and tatweel and unify alef variants. Matching only."""
    text = _ARABIC_MARKS.sub("", text).replace(_TATWEEL, "")
    return text.translate(_ALEF_VARIANTS)


def fold_diacritics(text: str) -> str:
    """Remove combining marks (é -> e, ü -> u). Arabic marks are handled separately first."""
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def normalize_text(text: str) -> str:
    """Case-fold and remove diacritics, Arabic marks and alef variants."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return fold_diacritics(normalize_arabic(text))


def cjk_bigrams(run: str) -> list[str]:
    """Overlapping character pairs of a CJK run; a one-character run stays as it is."""
    if len(run) < 2:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Normalised tokens: words for alphabetic scripts, character pairs for Chinese."""
    tokens: list[str] = []
    for match in _TOKEN.findall(normalize_text(text)):
        tokens.extend(cjk_bigrams(match) if _IS_CJK.match(match) else [match])
    return tokens


def index_text(text: str) -> str:
    """What to store in the FTS table for `text` (space-separated tokens)."""
    return " ".join(tokenize(text))


def fts_query(text: str) -> str:
    """An FTS5 MATCH expression requiring every token (quoted, so no operator can be injected).

    Returns "" when the text has no tokens; callers must not run an empty MATCH.
    """
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokenize(text))
