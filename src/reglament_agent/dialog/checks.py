"""Deterministic verification of model claims against cited sentences."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .lexicon import (
    NEGATION_PREFIXES,
    NEGATION_WORDS,
    NUMBER_WORDS,
    QUALIFIERS,
    action_stem,
    canonical_role,
    find_roles,
    fold,
    words,
)

NEG_WINDOW = 6


@dataclass(frozen=True)
class SentenceRef:
    doc_id: str
    point: str
    sentence: int


@dataclass(frozen=True)
class Evidence:
    ref: SentenceRef
    text: str
    column: str | None = None


@dataclass(frozen=True)
class Claim:
    text: str
    ref: SentenceRef
    actor: str | None = None
    action: str | None = None
    object: str | None = None
    negated: bool = False
    qualifier: str | None = None


@dataclass
class Verdict:
    claim: Claim
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    kept: list[Claim]
    dropped: list[Verdict]

    @property
    def status(self) -> str:
        if not self.kept:
            return "none"
        return "partial" if self.dropped else "full"


_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+|[.;!?]")


def _tokenize(text: str) -> tuple[list[str], list[int], list[str]]:
    """Folded words, sentence boundaries and the punctuation immediately before each word."""
    text = fold(text)
    toks, clauses, gaps = [], [], []
    clause = previous_end = 0
    for match in _TOKEN.finditer(text):
        token = match[0]
        if token in ".;!?":
            clause += 1
        else:
            toks.append(token)
            clauses.append(clause)
            gaps.append("".join(text[previous_end:match.start()].split()))
            previous_end = match.end()
    return toks, clauses, gaps


def _is_neg(tok: str) -> bool:
    return tok in NEGATION_WORDS or tok.startswith(NEGATION_PREFIXES)


def _action_positions(toks: list[str], action: str) -> list[int]:
    aw = words(action)
    if not aw:
        return []
    first = action_stem(aw[0])
    pos = [i for i, t in enumerate(toks) if action_stem(t) == first]
    rest = {action_stem(w) for w in aw[1:]}
    if rest and not rest <= {action_stem(t) for t in toks}:
        return []
    return pos


_LIMIT_PREFIXES = ("реже", "чаще", "позднее", "ранее", "менее", "более", "свыше",
                   "превыша", "только")


def _is_limit_ne(toks: list[str], j: int) -> bool:
    """«не» introducing a comparative/limit phrase («не реже», «не более»...)."""
    return toks[j] == "не" and j + 1 < len(toks) and toks[j + 1].startswith(_LIMIT_PREFIXES)


def _negated_at(toks: list[str], clauses: list[int], i: int) -> bool:
    if any(_is_neg(toks[j]) and not _is_limit_ne(toks, j) for j in range(max(0, i - NEG_WINDOW), i)):
        return True
    return any(
        j > i and clauses[j] == clauses[i] and toks[j].startswith(NEGATION_PREFIXES)
        for j in range(i + 1, len(toks))
    )


def _numbers(text: str) -> set[str]:
    t = fold(text)
    nums = {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", t)}
    nums |= {NUMBER_WORDS[w] for w in words(t) if w in NUMBER_WORDS}
    return nums


def _qualifier(q: str) -> str:
    q = " ".join(words(q))
    return QUALIFIERS.get(q, q)


def _dist(a: int, span: tuple[int, int, str]) -> int:
    s, e, _ = span
    return min(abs(a - s), abs(a - (e - 1)))


_GENITIVE = ("а", "я", "ов", "ев", "ей", "ы")


def _object_span(toks, spans, p):
    """Role span in the genitive that directly follows the verbal noun at position p, if any."""
    if not toks[p].endswith(("ние", "тие")):
        return None
    for s in spans:
        if s[0] == p + 1 and toks[s[1] - 1].endswith(_GENITIVE):
            return s
    return None


def _relative_subjects(toks, gaps, spans, predicate):
    """Resolve an immediately governing «который проводит» to a role or explicit role alternatives."""
    relative = predicate - 1
    if relative >= 0 and toks[relative] == "не":
        relative -= 1
    if (relative < 0 or toks[relative] not in {"который", "которая", "которые"}
            or not re.search(r"(?:ет|ит|ут|ют|ат|ят)(?:ся)?$", toks[predicate])):
        return None
    # An interrupted relative clause gives no licence to borrow an earlier subject.
    if gaps[relative] not in {",", "),"} or any(gaps[relative + 1:predicate + 1]):
        return set()
    by_end = {end: (start, role) for start, end, role in spans}

    def preceding_role(index):
        previous = by_end.get(index)
        if previous and gaps[index].startswith(")"):
            # A parenthetical abbreviation is only an alias for the immediately preceding same role.
            alias_start, alias = previous
            full = by_end.get(alias_start)
            if gaps[alias_start] != "(" or not full or full[1] != alias:
                return None
            return full
        return previous

    previous = preceding_role(relative)
    if not previous:
        # «Отчёт, который проверяет РП»: the pronoun can be an object with an explicit subject.
        return None
    roles = set()
    while previous:
        start, role = previous
        roles.add(role)
        if gaps[start] not in {"/", ")/"}:
            break
        previous = preceding_role(start)
    return roles


def _check(claim: Claim, ev: Evidence | None) -> list[str]:
    if ev is None:
        return ["unknown_ref"]
    reasons: list[str] = []
    toks, clauses, gaps = _tokenize(ev.text)
    actor_spans: list[tuple[int, int, str]] = []
    all_spans: list[tuple[int, int, str]] = []
    actor_ok = action_ok = True
    positions: list[int] = []

    if claim.actor:
        canon, pats = canonical_role(claim.actor)
        all_spans = find_roles(toks, pats)
        actor_spans = [s for s in all_spans if s[2] == canon]
        if not actor_spans:
            actor_ok = False
            reasons.append("actor_missing")
    if claim.action:
        positions = _action_positions(toks, claim.action)
        if not positions:
            action_ok = False
            reasons.append("action_missing")

    if claim.actor and claim.action and actor_ok and action_ok:
        others = [s for s in all_spans if s[2] != actor_spans[0][2]]

        def bound_at(p):
            subjects = _relative_subjects(toks, gaps, all_spans, p)
            if subjects is not None:
                return actor_spans[0][2] in subjects
            # «назначение Архитектора»: a role in the genitive right after a verbal noun is the object of the act,
            # neither a competing nor a valid performer.
            # Only for the very noun the claim names, not for a synonym stem that happens to sit there.
            exact = toks[p][:5] == (words(claim.action) or [""])[0][:5]
            obj = _object_span(toks, all_spans, p) if exact else None
            mine = [s for s in actor_spans if s != obj]
            rivals = [s for s in others if s != obj]
            return bool(mine) and min(_dist(p, s) for s in mine) <= min((_dist(p, s) for s in rivals), default=10**9)

        bound = any(bound_at(p) for p in positions)
        if not bound:
            reasons.append("actor_not_bound")

    if claim.action and action_ok:
        neg = any(_negated_at(toks, clauses, p) for p in positions)
        if neg != claim.negated:
            reasons.append("negation_mismatch")

    if ev.column and claim.qualifier and _qualifier(ev.column) != _qualifier(claim.qualifier):
        reasons.append("column_mismatch")

    if not _numbers(claim.text) <= _numbers(ev.text):
        reasons.append("number_missing")
    return reasons


def verify_claims(claims: list[Claim], evidence: dict[SentenceRef, Evidence]) -> VerificationResult:
    kept: list[Claim] = []
    dropped: list[Verdict] = []
    for c in claims:
        reasons = _check(c, evidence.get(c.ref))
        if reasons:
            dropped.append(Verdict(c, False, reasons))
        else:
            kept.append(c)
    return VerificationResult(kept, dropped)


def decide(model_outcome: str, result: VerificationResult) -> tuple[str, bool]:
    if model_outcome in ("answer", "variants"):
        if result.status == "none":
            return "no_basis", False
        if result.status == "partial":
            return model_outcome, True
    return model_outcome, False
