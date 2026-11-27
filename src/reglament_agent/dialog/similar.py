"""Reuse of a cached answer for a similar phrasing (dialog-p01). Off by default.

A wrong hit answers a different question, which is worse than no answer. So a cached answer is reused
only if the scope, the model and the active publications are the same (the cache context), the question
embeddings are close, and the questions do not differ in anything that changes the answer: numbers,
negations, department and stage words, the question word. Any doubt is a miss.
"""

import math
import os
import re

from . import context, state
from .lexicon import fold, words

DEFAULT_MIN_COSINE = 0.97
CANDIDATES = 200  # newest entries of one context that are compared

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_NUMBER_WORD = re.compile(
    r"(?:ноль|нуля|один|одн\w*|дв[аеу]|двух\w*|тр[иё]х?|трем|тремя|четыр\w*|пят\w*|шест\w*|сем[ьи]|семью|"
    r"восем\w*|девят\w*|десят\w*|одиннадцат\w*|двенадцат\w*|двадцат\w*|тридцат\w*|сорок\w*|"
    r"сто|ста|сотн\w*|тысяч\w*|миллион\w*|перв\w*|втор\w*|трет\w*|четв[её]рт\w*)"
)
_NEGATIONS = {"не", "ни", "нет", "без", "безо", "нельзя", "запрещено", "запрещается", "никогда", "никто", "ничего"}
_QUESTION_WORDS = {
    "кто": "кто", "кого": "кто", "кому": "кто", "кем": "кто", "ком": "кто",
    "когда": "когда", "сколько": "сколько", "скольки": "сколько", "скольким": "сколько",
    "где": "где", "куда": "где", "откуда": "где",
    "как": "как", "какой": "какой", "какая": "какой", "какое": "какой", "какие": "какой", "каких": "какой",
    "какого": "какой", "какую": "какой", "каком": "какой", "какому": "какой",
    "почему": "почему", "зачем": "зачем", "чей": "чей", "что": "что", "чем": "что", "чего": "что",
    "который": "какой", "которая": "какой", "которое": "какой", "которые": "какой",
}
_ACRONYM = re.compile(r"\b[A-ZА-ЯЁ]{2,}\b")


def settings(env=None):
    """(enabled, minimum cosine) from CACHE_SIMILAR_ENABLED (default off) and CACHE_SIMILAR_MIN_COSINE."""
    env = os.environ if env is None else env
    enabled = env.get("CACHE_SIMILAR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        threshold = float(env.get("CACHE_SIMILAR_MIN_COSINE", DEFAULT_MIN_COSINE))
    except ValueError:
        threshold = DEFAULT_MIN_COSINE
    return enabled, threshold


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def signature(text, departments):
    """What must not differ between two questions for one answer to serve both."""
    tokens = words(text)
    numbers = {n.replace(",", ".") for n in _NUMBER.findall(text)}
    numbers |= {t for t in tokens if _NUMBER_WORD.fullmatch(t)}
    return {
        "numbers": frozenset(numbers),
        "negations": frozenset(t for t in tokens if t in _NEGATIONS),
        "departments": frozenset(context.departments_in(text, departments)),
        "stage": context.stage_in(text) or ("presale" if re.search(r"пресейл|предпродаж", fold(text)) else None),
        "acronyms": frozenset(a.casefold() for a in _ACRONYM.findall(text)),
        "asks": frozenset(_QUESTION_WORDS[t] for t in tokens if t in _QUESTION_WORDS),
    }


def same_condition(a, b, departments):
    return signature(a, departments) == signature(b, departments)


def find(conn, ctx, question, vector, departments, min_cosine):
    """The best cached answer of the same context for a similar question, as (answer, request id, cosine)."""
    if not vector:
        return None
    rows = conn.execute(
        "SELECT answer, id, restated, cache_embedding FROM dialog_requests "
        "WHERE cache_context=%s AND cache_embedding IS NOT NULL AND cache_key IS NOT NULL "
        "ORDER BY created_at DESC LIMIT %s",
        (ctx, CANDIDATES),
    ).fetchall()
    best = None
    for answer, ident, restated, stored in rows:
        score = cosine(vector, stored)
        if score < min_cosine or (best and score <= best[0]):
            continue
        if not same_condition(question, restated, departments):
            continue
        copy = state.shareable(state.answers.validate_python(answer))
        if copy:
            best = (score, copy, str(ident))
    return (best[1], best[2], best[0]) if best else None
