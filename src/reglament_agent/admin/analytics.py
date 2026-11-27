"""Analytics over the request log (`dialog_requests`): read-only, generic, no hard-coded questions."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

log = logging.getLogger(__name__)

PERIODS = {"7": ("7 дней", 7), "30": ("30 дней", 30), "all": ("всё время", None)}
DEFAULT_PERIOD = "7"

# Пороги рекомендаций (меняются здесь).
REC_MIN_SECTION_EVENTS = 5   # обращений + 👎 + уточнений по одному разделу за период
REC_MIN_REPEATS = 5          # одинаковых по смыслу вопросов за период
SERVICE_DOMAIN = "@service.local"  # служебные прогоны (проверочные вопросы) не попадают в аналитику
GROUP_MIN_COSINE_DEFAULT = 0.90  # ANALYTICS_GROUP_MIN_COSINE: порог косинусной близости для группы перефразировок
TOP_CANDIDATES = 3           # сколько первых кандидатов поиска считать «затронутыми» разделами

OUTCOMES = {
    "answer": "Ответ", "clarify": "Уточнение", "variants": "Варианты по подразделениям",
    "no_basis": "Нет в регламентах", "external_doc": "Нужен внешний регламент",
    "conflict": "Положения расходятся", "low_confidence": "Требует подтверждения",
    "expert_answer": "Ответ службы качества", "off_topic": "Не по регламентам", "error": "Ошибка",
}
TICKET_OUTCOMES = ("no_basis", "external_doc", "conflict", "low_confidence")
UNANSWERED = (*TICKET_OUTCOMES, "off_topic", "error")

DROPPED = {
    "unknown_ref": "ссылка на пункт, которого нет среди найденных",
    "actor_missing": "в пункте регламента не найден указанный исполнитель",
    "action_missing": "в пункте регламента не найдено указанное действие",
    "actor_not_bound": "действие в пункте относится к другому исполнителю",
    "negation_mismatch": "утверждение и пункт расходятся в отрицании («не»)",
    "column_mismatch": "утверждение относится к другому столбцу таблицы",
    "number_missing": "числа из утверждения нет в тексте пункта",
    "topic_mismatch": "пункт относится к другой теме вопроса",
}

_STOP = frozenset("а в во и или на по к с со у о об от до для за из ли бы как что это ее его их мне мой нам "
                  "нужно можно же то так там тут при про над под через ещё еще какой какая какие "
                  "каков сколько где когда".split())
_WORD = re.compile(r"[\w]+", re.UNICODE)


def normalize(text: str) -> str:
    tokens = {t for t in _WORD.findall((text or "").casefold().replace("ё", "е")) if t not in _STOP}
    return " ".join(sorted(tokens))


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])[1]
    return (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None


def fetch(conn, period: str, limit: int | None = None) -> list[dict]:
    since = period_start(period)
    sql = ("SELECT id, user_email, dialog_id, question, restated, answer, trace, created_at, rating, feedback, "
           "cache_key FROM dialog_requests WHERE user_email NOT LIKE '%%" + SERVICE_DOMAIN + "' "
           + ("AND created_at >= %s " if since else "")
           + "ORDER BY created_at DESC, id" + (f" LIMIT {int(limit)}" if limit else ""))
    rows = conn.cursor(row_factory=dict_row).execute(sql, (since,) if since else ()).fetchall()
    attach_tickets(conn, rows, since)
    return rows


def ticket_only_requests(conn, rows: list[dict], period: str) -> list[dict]:
    """Requests older than the period whose tickets were created inside it: used only to count tickets and
    attribute them to sections (questions, outcomes, ratings and economics stay filtered by request date)."""
    since = period_start(period)
    if since is None:
        return []
    known = [r["id"] for r in rows]
    extra = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT r.id, r.user_email, r.dialog_id, r.question, r.restated, r.answer, r.trace, r.created_at, "
        "r.rating, r.feedback, r.cache_key FROM dialog_requests r JOIN tickets t ON t.user_email=r.user_email AND "
        "(t.message_id = 'feedback:' || r.id::text OR (r.dialog_id=t.dialog_id AND r.message_id=t.message_id)) "
        "WHERE t.created_at >= %s AND r.created_at < %s AND r.user_email NOT LIKE '%%" + SERVICE_DOMAIN
        + "' AND NOT (r.id = ANY(%s))", (since, since, known)).fetchall()
    attach_tickets(conn, extra, since, only_linked=True)
    seen = {n for r in rows for n in r["tickets"]}
    out = []
    for r in extra:
        r["tickets"] = [n for n in r["tickets"] if n not in seen]
        seen.update(r["tickets"])
        if r["tickets"]:
            out.append(r)
    return out


def attach_tickets(conn, rows: list[dict], since, only_linked: bool = False) -> None:
    """r["tickets"]: numbers of all tickets of the request, deduplicated: the one named in the answer plus
    tickets linked by the same message or by a forwarded 👎 (`feedback:<id>`), created within the period."""
    linked: dict = defaultdict(list)
    if rows:
        found = conn.execute(
            "SELECT r.id, t.number FROM tickets t JOIN dialog_requests r ON r.user_email=t.user_email AND "
            "(t.message_id = 'feedback:' || r.id::text OR (r.dialog_id=t.dialog_id AND r.message_id=t.message_id)) "
            "WHERE r.id = ANY(%s)" + (" AND t.created_at >= %s" if since else "") + " ORDER BY t.id",
            [[r["id"] for r in rows], *([since] if since else [])]).fetchall()
        for rid, number in found:
            linked[rid].append(number)
    for r in rows:
        own = None if only_linked else ticket_number(r)
        r["tickets"] = list(dict.fromkeys([*([own] if own else []), *linked.get(r["id"], [])]))


def n_tickets(r: dict) -> int:
    """Tickets of a request; a ticket-creating outcome counts as one even if its row is not found."""
    return max(len(r.get("tickets") or []), 1 if outcome(r) in TICKET_OUTCOMES else 0)


# --- per-request facts -----------------------------------------------------------------------

def outcome(r: dict) -> str:
    return (r["answer"] or {}).get("outcome") or "error"


def elapsed(r: dict) -> int | None:
    v = (r["trace"] or {}).get("elapsed_ms")
    return int(v) if isinstance(v, (int, float)) else None


def cache_hit(r: dict) -> bool:
    return bool((r["answer"] or {}).get("from_cache")) or "cache_hit" in ((r["trace"] or {}).get("steps") or [])


def chat_calls(r: dict) -> int:
    return sum(1 for c in (r["trace"] or {}).get("calls") or [] if c.get("kind", "chat") == "chat")


def tokens(r: dict) -> tuple[int, int, int]:
    calls = (r["trace"] or {}).get("calls") or []
    return (sum(int(c.get("prompt_tokens") or 0) for c in calls),
            sum(int(c.get("completion_tokens") or 0) for c in calls), len(calls))


def ticket_number(r: dict) -> str | None:
    return ((r["answer"] or {}).get("ticket") or {}).get("number")


def _sources(answer: dict) -> list[dict]:
    out = []
    for st in answer.get("statements") or []:
        out += st.get("sources") or []
    for v in answer.get("variants") or []:
        for st in v.get("statements") or []:
            out += st.get("sources") or []
    return [s for s in out if s.get("doc_id") and s.get("point") and not s.get("faq_id")]


def _candidates(r: dict) -> list[dict]:
    return [c for c in ((r["trace"] or {}).get("candidates") or [])[:TOP_CANDIDATES] if c.get("doc_id") and c.get("point")]


def _ref(d: dict) -> tuple:
    return (d["doc_id"], d.get("version_no") or d.get("version"), d["point"])


def resolve_sections(conn, refs: set[tuple]) -> dict[tuple, tuple]:
    """(doc_id, version_no, point) -> (doc_id, doc_title, section_number, section_title)."""
    docs = sorted({r[0] for r in refs})
    if not docs:
        return {}
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT p.doc_id, d.title, v.version_number, p.number, p.section_number, p.section_title, "
        "(d.active_version_id = v.version_id) FROM points p JOIN document_versions v ON v.version_id=p.version_id "
        "JOIN documents d ON d.doc_id=p.doc_id WHERE p.doc_id = ANY(%s)", (docs,)).fetchall()
    exact, active = {}, {}
    for doc, title, vno, number, sec, sec_title, is_active in rows:
        exact[(doc, vno, number)] = (doc, title, sec, sec_title)
        if is_active:
            active[(doc, number)] = (doc, title, sec, sec_title)
    titles = {d: t for d, t, *_ in rows}
    out = {}
    for ref in refs:
        doc, vno, number = ref
        out[ref] = (exact.get((doc, vno, number)) or active.get((doc, number))
                    or (doc, titles.get(doc, doc), number.split(".")[0], ""))
    return out


# --- aggregation -----------------------------------------------------------------------------

def _pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, -(-int(q * 100) * len(s) // 100) - 1)]


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def group_hash(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def repeated(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = normalize(r["restated"] or r["question"]) or normalize(r["question"])
        if key:
            groups[key].append(r)
    out = [{"key": k, "hash": group_hash(k), "count": len(v), "example": v[0]["question"], "users": len({x["user_email"] for x in v}),
            "variants": sorted({x["question"] for x in v})[:5]} for k, v in groups.items() if len(v) >= 2]
    return sorted(out, key=lambda g: (-g["count"], g["key"]))


# --- semantic grouping of repeated questions ---------------------------------------------------

_NEGATIONS = frozenset({"не", "нет", "без", "нельзя", "ни", "никогда"})
_QWORDS = {"кто": "кто", "кого": "кто", "кому": "кто", "когда": "когда", "сколько": "сколько", "где": "где",
           "как": "как", "куда": "куда", "почему": "почему", "зачем": "зачем", "какой": "какой", "какая": "какой",
           "какое": "какой", "какие": "какой", "какого": "какой", "каких": "какой"}
# основы слов-подразделений; дополняются названиями подразделений из таблицы documents
_DEPT_STEMS = ("дирекц", "департамент", "отдел", "служб", "бухгалтер", "юридич", "юрист", "закуп", "кадр",
               "безопасност", "казначейств", "логистик", "финанс", "управлени", "комплаенс", "охран")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def group_min_cosine() -> float:
    try:
        v = float(os.environ.get("ANALYTICS_GROUP_MIN_COSINE", GROUP_MIN_COSINE_DEFAULT))
    except ValueError:
        return GROUP_MIN_COSINE_DEFAULT
    return v if 0 < v <= 1 else GROUP_MIN_COSINE_DEFAULT


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").casefold().replace("ё", "е"))


def guard_signature(text: str, dept_vocab: frozenset[str] = frozenset()) -> tuple:
    """Mandatory conditions of a question: two questions may be grouped only if all of them coincide."""
    words = _words(text)
    nums = frozenset(n.replace(",", ".") for n in _NUM.findall(text or ""))
    depts = frozenset(w for w in words if w in dept_vocab or any(w.startswith(st) for st in _DEPT_STEMS))
    return (nums, frozenset(w for w in words if w in _NEGATIONS), depts,
            frozenset(_QWORDS[w] for w in words if w in _QWORDS))


def dept_vocabulary(conn) -> frozenset[str]:
    try:
        found = conn.execute("SELECT DISTINCT department FROM documents WHERE department IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 - the vocabulary is optional
        return frozenset()
    return frozenset(w for (d,) in found for w in _words(d) if len(w) >= 2)


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _clean(text: str) -> str:
    return " ".join(_words(text))


def _group_dict(key: str, members: list[dict]) -> dict:
    out = Counter(outcome(r) for r in members)
    ticket = sum(1 for r in members if n_tickets(r) > 0)
    return {"key": key, "hash": group_hash(key), "count": len(members), "example": members[0]["question"],
            "users": len({x["user_email"] for x in members}),
            "variants": sorted({x["question"] for x in members})[:5],
            "n_variants": len({x["question"] for x in members}),
            "mix": {"answered": sum(out[o] for o in ("answer", "variants", "expert_answer")),
                    "no_basis": out["no_basis"], "ticket": ticket},
            "row_ids": [x["id"] for x in members]}


def repeated_groups(rows: list[dict], embedder=None, *, min_cosine: float | None = None,
                    dept_vocab: frozenset[str] = frozenset(), cache: dict | None = None) -> list[dict]:
    """Groups of similar questions (>= 2 requests). Exact groups (`repeated`) are merged greedily by cosine
    similarity of their embeddings, but only when the mandatory conditions (numbers, negations, departments,
    question word) coincide. Any embedder failure -> plain exact grouping."""
    exact: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = normalize(r["restated"] or r["question"]) or normalize(r["question"])
        if key:
            exact[key].append(r)
    units = sorted(exact.items(), key=lambda kv: (-len(kv[1]), kv[0]))  # largest first: it becomes the representative
    clusters: list[tuple[str, list[dict]]] = [(k, list(v)) for k, v in units]
    if embedder is not None and len(units) > 1:
        try:
            threshold = group_min_cosine() if min_cosine is None else min_cosine
            cache = {} if cache is None else cache
            text_of = {k: _clean(v[0]["restated"] or v[0]["question"]) for k, v in units}
            todo = sorted({t for t in text_of.values() if t not in cache})
            if todo:
                vectors = embedder.embed(todo)
                if len(vectors) != len(todo):
                    raise ValueError("embedder returned a wrong number of vectors")
                cache.update(zip(todo, vectors))
            sig = {k: guard_signature(v[0]["restated"] or v[0]["question"], dept_vocab) for k, v in units}
            clusters = []
            reps: list[str] = []
            for k, v in units:
                for i, rep in enumerate(reps):
                    if sig[k] == sig[rep] and _cosine(cache[text_of[k]], cache[text_of[rep]]) >= threshold:
                        clusters[i][1].extend(v)
                        break
                else:
                    reps.append(k)
                    clusters.append((k, list(v)))
        except Exception:  # noqa: BLE001 - the page must never fail because of the embedder
            log.warning("analytics: embedder unavailable, exact grouping used", exc_info=True)
            clusters = [(k, list(v)) for k, v in units]
    out = []
    for key, members in clusters:
        if len(members) >= 2:
            members.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
            out.append(_group_dict(key, members))
    return sorted(out, key=lambda g: (-g["count"], g["key"]))


def build_groups(conn, rows: list[dict]) -> list[dict]:
    """Semantic groups with the embedder chosen by the environment (EMBEDDER); exact grouping on any failure."""
    try:
        from reglament_agent.ingest.embedding import open_embedder
        vocab = dept_vocabulary(conn)
        with open_embedder() as emb:
            return repeated_groups(rows, emb, dept_vocab=vocab)
    except Exception:  # noqa: BLE001
        log.warning("analytics: embedder unavailable, exact grouping used", exc_info=True)
        return repeated_groups(rows)


def unanswered(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        o = outcome(r)
        if o in UNANSWERED:
            out.setdefault(o, []).append({"id": r["id"], "question": r["question"], "created_at": r["created_at"],
                                          "ticket": r.get("tickets") or []})
    return {o: out[o] for o in UNANSWERED if o in out}


def sections_stats(conn, rows: list[dict], extra: list[dict] = ()) -> tuple[list[dict], list[dict]]:
    """(problem sections, demand) — both keyed by document and section."""
    per_req = []  # (request, set(refs) shown in answer, set(refs) of candidates)
    all_refs: set[tuple] = set()
    for r in [*rows, *extra]:  # extra: only for their tickets
        shown = {_ref(s) for s in _sources(r["answer"] or {})}
        cands = {_ref(c) for c in _candidates(r)}
        per_req.append((r, shown, cands))
        all_refs |= shown | cands
    sec = resolve_sections(conn, all_refs)
    def key(ref):
        doc, doc_title, number, title = sec[ref]
        return (doc, number, doc_title, title)

    demand: Counter = Counter()
    problems: dict[tuple, Counter] = defaultdict(Counter)
    for r, shown, cands in per_req:
        in_period = not any(r is e for e in extra)
        if in_period:
            for k in {key(x) for x in shown}:
                demand[k] += 1
        o = outcome(r)
        touched = {key(x) for x in (shown if r["rating"] is False and shown else cands if r["rating"] is False else ())}
        for k in touched if in_period else ():
            problems[k]["down"] += 1
        n = n_tickets(r)
        for k in {key(x) for x in (cands or shown)}:
            problems[k]["tickets"] += n
            if o == "clarify" and in_period:
                problems[k]["clarify"] += 1
    prob = [{"doc_id": k[0], "section": k[1], "doc_title": k[2], "title": k[3], "tickets": c["tickets"],
             "down": c["down"], "clarify": c["clarify"], "total": c["tickets"] + c["down"] + c["clarify"]}
            for k, c in problems.items()]
    prob.sort(key=lambda p: (-p["total"], p["doc_id"], p["section"]))
    dem = [{"doc_id": k[0], "section": k[1], "doc_title": k[2], "title": k[3], "count": n} for k, n in demand.items()]
    dem.sort(key=lambda d: (-d["count"], d["doc_id"], d["section"]))
    return prob, dem


def recommendations(problems: list[dict], repeats: list[dict], period: str) -> list[str]:
    label = {"7": "за 7 дней", "30": "за 30 дней", "all": "за всё время"}.get(period, "за период")
    out = []
    for p in problems:
        if p["total"] >= REC_MIN_SECTION_EVENTS:
            title = f" «{p['title']}»" if p["title"] else ""
            out.append(f"Раздел {p['section']}{title} документа «{p['doc_title']}»: {p['total']} обращений "
                       f"{label} (тикеты {p['tickets']}, 👎 {p['down']}, уточнения {p['clarify']}) — "
                       "дополнить регламент или FAQ")
    for g in repeats:
        if g["count"] >= REC_MIN_REPEATS:
            out.append(f"Вопрос «{g['example']}» задан {g['count']} раз {label} — добавить в FAQ")
    return out


def economics(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[outcome(r)].append(r)
    out = []
    for o in [*OUTCOMES, *sorted(set(by) - set(OUTCOMES))]:
        rs = by.get(o)
        if not rs:
            continue
        tk = [tokens(r) for r in rs]
        ms = [e for e in map(elapsed, rs) if e is not None]
        n = len(rs)
        out.append({"outcome": o, "label": OUTCOMES.get(o, o), "requests": n,
                    "avg_prompt": sum(t[0] for t in tk) / n, "avg_completion": sum(t[1] for t in tk) / n,
                    "avg_calls": sum(t[2] for t in tk) / n,
                    "avg_chat_calls": sum(map(chat_calls, rs)) / n, "cache_share": sum(map(cache_hit, rs)) / n,
                    "median_ms": median(ms), "p90_ms": _pct(ms, 0.9)})
    return out


def summary(conn, period: str, groups: list[dict] | None = None) -> dict:
    rows = fetch(conn, period)
    counts = Counter(outcome(r) for r in rows)
    ms = [e for e in map(elapsed, rows) if e is not None]
    extra = ticket_only_requests(conn, rows, period)
    problems, demand = sections_stats(conn, rows, extra)
    rep = build_groups(conn, rows) if groups is None else groups
    all_tk = [tokens(r) for r in rows]
    return {
        "period": period, "period_label": PERIODS[period][0], "total": len(rows), "counts": counts,
        "outcome_rows": [(k, OUTCOMES[k], counts.get(k, 0)) for k in OUTCOMES],
        "ticket_outcomes": sum(map(n_tickets, [*rows, *extra])),
        "median_ms": median(ms), "up": sum(1 for r in rows if r["rating"] is True),
        "down": sum(1 for r in rows if r["rating"] is False),
        "repeated": rep, "repeated_exact": repeated(rows), "unanswered": unanswered(rows), "problems": problems, "demand": demand,
        "recommendations": recommendations(problems, rep, period),
        "economics": economics(rows), "cache_hits": sum(map(cache_hit, rows)),
        "tokens_total": (sum(t[0] for t in all_tk), sum(t[1] for t in all_tk)),
    }


# --- CSV -------------------------------------------------------------------------------------

def _cell(v):
    if isinstance(v, str) and v and v[0] in "=+-@\t\r":  # formula injection in spreadsheet apps
        return "'" + v
    return v


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for row in rows:
        w.writerow([_cell(v) for v in row])
    return "﻿" + buf.getvalue()


def requests_csv(rows: list[dict], groups: list[dict] = ()) -> str:
    gid = {i: g["hash"] for g in groups for i in g["row_ids"]}

    def line(r):
        p, c, n = tokens(r)
        return [r["id"], r["created_at"].isoformat(), r["user_email"], outcome(r), r["question"], r["restated"],
                ";".join(r.get("tickets") or []), "" if r["rating"] is None else ("up" if r["rating"] else "down"),
                r["feedback"] or "", int(cache_hit(r)), elapsed(r) if elapsed(r) is not None else "", p, c, n, chat_calls(r),
                gid.get(r["id"], "")]
    return _csv(["id", "created_at", "user_email", "outcome", "question", "restated", "ticket", "rating", "feedback",
                 "cache_hit", "elapsed_ms", "prompt_tokens", "completion_tokens", "model_calls", "chat_calls", "group_id"], map(line, rows))


def economics_csv(rows: list[dict]) -> str:
    def f(v):
        return "" if v is None else round(v, 2)
    return _csv(["outcome", "requests", "avg_prompt_tokens", "avg_completion_tokens", "avg_model_calls", "avg_chat_calls",
                 "cache_hit_share", "median_ms", "p90_ms"],
                [[e["outcome"], e["requests"], f(e["avg_prompt"]), f(e["avg_completion"]), f(e["avg_calls"]), f(e["avg_chat_calls"]),
                  f(e["cache_share"]), f(e["median_ms"]), f(e["p90_ms"])] for e in economics(rows)])


# --- journal ---------------------------------------------------------------------------------

def request_detail(conn, request_id: str) -> dict | None:
    r = conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM dialog_requests WHERE id::text=%s", (request_id,)).fetchone()
    if r is None:
        return None
    trace, answer = r["trace"] or {}, r["answer"] or {}
    p, c, n = tokens(r)
    attach_tickets(conn, [r], None)
    ticket = r["tickets"]
    return {"r": r, "outcome": outcome(r), "outcome_label": OUTCOMES.get(outcome(r), outcome(r)),
            "steps": trace.get("steps") or [], "candidates": trace.get("candidates") or [],
            "calls": trace.get("calls") or [], "chat_calls": chat_calls(r),
            "dropped": [(d, DROPPED.get(d, d)) for d in trace.get("dropped") or []], "elapsed_ms": elapsed(r),
            "prompt": p, "completion": c, "n_calls": n, "cache_hit": cache_hit(r), "ticket": ticket,
            "sources": _sources(answer), "summary": answer.get("summary", "")}
