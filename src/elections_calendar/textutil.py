"""String normalisation shared by parsers and the region matcher."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Words that describe the kind of territory rather than name it.
_GENERIC_REGION_WORDS = {
    "autonomous", "community", "comunidad", "comunitat", "region", "regione", "region of",
    "province", "provincia", "state", "free", "land", "principality", "of", "the", "de", "del",
    "di", "la", "el", "foral", "chartered", "islands", "island", "city",
    "collectivity", "territorial", "county", "voivodeship", "self-governing", "and",
}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def normalize(text: str) -> str:
    """Lower-case, accent-free, punctuation-free, single-spaced."""
    text = strip_accents(text or "").lower()
    text = text.replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("ł", "l")
    text = re.sub(r"[‐-―\-/_.,;:'’`()\"]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def region_tokens(name: str) -> set[str]:
    tokens = set(normalize(name).split())
    core = {t for t in tokens if t not in _GENERIC_REGION_WORDS}
    return core or tokens


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-")
    return slug[:max_len].strip("-")


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def same_region_name(a: str, b: str) -> bool:
    """Loose comparison: 'Community of Madrid' == 'Madrid', 'Castile-La Mancha' ~ 'Castilla–La Mancha'."""
    if not a or not b:
        return False
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    ta, tb = region_tokens(a), region_tokens(b)
    if ta == tb:
        return True
    # No subset matching: "Saxony" must not match "Lower Saxony".
    return similar(" ".join(sorted(ta)), " ".join(sorted(tb))) >= 0.88
