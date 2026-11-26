"""Text normalisation shared by parser, storage and search."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
# Split after . ! ? ; followed by space and an uppercase letter / digit / quote, and on newlines.
_SENT = re.compile(r"(?<=[.!?;])\s+(?=[«\"(A-ZА-ЯЁ0-9-])|\n+")


def normalize(text: str) -> str:
    """NFC, unify quotes/dashes/non-breaking spaces, collapse whitespace."""
    t = unicodedata.normalize("NFC", text)
    t = t.replace(" ", " ").replace("‑", "-").replace("–", "–")
    return _WS.sub(" ", t).strip()


def content_hash(text: str) -> str:
    """Stable hash of a point's text (ADR-010): sha256 of normalize(text)."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def split_sentences(text: str) -> list[str]:
    parts = [normalize(p) for p in _SENT.split(text)]
    return [p for p in parts if p]
