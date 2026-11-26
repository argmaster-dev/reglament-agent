"""Analytics over the request log (`dialog_requests`): read-only, generic, no hard-coded questions."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

PERIODS = {"7": ("7 дней", 7), "30": ("30 дней", 30), "all": ("всё время", None)}
DEFAULT_PERIOD = "7"

# Пороги рекомендаций (меняются здесь).
REC_MIN_SECTION_EVENTS = 5   # обращений + 👎 + уточнений по одному разделу за период
REC_MIN_REPEATS = 5          # одинаковых по смыслу вопросов за период
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
           "cache_key FROM dialog_requests " + ("WHERE created_at >= %s " if since else "")
           + "ORDER BY created_at DESC, id" + (f" LIMIT {int(limit)}" if limit else ""))
    return conn.cursor(row_factory=dict_row).execute(sql, (since,) if since else ()).fetchall()


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


def unanswered(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        o = outcome(r)
        if o in UNANSWERED:
            out.setdefault(o, []).append({"id": r["id"], "question": r["question"], "created_at": r["created_at"],
                                          "ticket": ticket_number(r)})
    return {o: out[o] for o in UNANSWERED if o in out}


def sections_stats(conn, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(problem sections, demand) — both keyed by document and section."""
    per_req = []  # (request, set(refs) shown in answer, set(refs) of candidates)
    all_refs: set[tuple] = set()
    for r in rows:
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
        for k in {key(x) for x in shown}:
            demand[k] += 1
        o = outcome(r)
        touched = {key(x) for x in (shown if r["rating"] is False and shown else cands if r["rating"] is False else ())}
        for k in touched:
            problems[k]["down"] += 1
        flows = []
        if ticket_number(r) or o in TICKET_OUTCOMES:
            flows.append("tickets")
        if o == "clarify":
            flows.append("clarify")
        for flow in flows:
            for k in {key(x) for x in cands}:
                problems[k][flow] += 1
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


def summary(conn, period: str) -> dict:
    rows = fetch(conn, period)
    counts = Counter(outcome(r) for r in rows)
    ms = [e for e in map(elapsed, rows) if e is not None]
    problems, demand = sections_stats(conn, rows)
    rep = repeated(rows)
    all_tk = [tokens(r) for r in rows]
    return {
        "period": period, "period_label": PERIODS[period][0], "total": len(rows), "counts": counts,
        "outcome_rows": [(k, OUTCOMES[k], counts.get(k, 0)) for k in OUTCOMES],
        "ticket_outcomes": sum(counts.get(k, 0) for k in TICKET_OUTCOMES),
        "median_ms": median(ms), "up": sum(1 for r in rows if r["rating"] is True),
        "down": sum(1 for r in rows if r["rating"] is False),
        "repeated": rep, "unanswered": unanswered(rows), "problems": problems, "demand": demand,
        "recommendations": recommendations(problems, rep, period),
        "economics": economics(rows), "cache_hits": sum(map(cache_hit, rows)),
        "tokens_total": (sum(t[0] for t in all_tk), sum(t[1] for t in all_tk)),
    }


# --- CSV -------------------------------------------------------------------------------------

def _cell(v):
    if isinstance(v, str) and v[:1] in "=+-@\t\r":  # formula injection in spreadsheet apps
        return "'" + v
    return v


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for row in rows:
        w.writerow([_cell(v) for v in row])
    return "﻿" + buf.getvalue()


def requests_csv(rows: list[dict]) -> str:
    def line(r):
        p, c, n = tokens(r)
        return [r["id"], r["created_at"].isoformat(), r["user_email"], outcome(r), r["question"], r["restated"],
                ticket_number(r) or "", "" if r["rating"] is None else ("up" if r["rating"] else "down"),
                r["feedback"] or "", int(cache_hit(r)), elapsed(r) if elapsed(r) is not None else "", p, c, n, chat_calls(r)]
    return _csv(["id", "created_at", "user_email", "outcome", "question", "restated", "ticket", "rating", "feedback",
                 "cache_hit", "elapsed_ms", "prompt_tokens", "completion_tokens", "model_calls", "chat_calls"], map(line, rows))


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
    ticket = None
    if ticket_number(r):
        ticket = conn.cursor(row_factory=dict_row).execute(
            "SELECT number, status, reason FROM tickets WHERE number=%s", (ticket_number(r),)).fetchone() \
            or {"number": ticket_number(r), "status": None, "reason": None}
    return {"r": r, "outcome": outcome(r), "outcome_label": OUTCOMES.get(outcome(r), outcome(r)),
            "steps": trace.get("steps") or [], "candidates": trace.get("candidates") or [],
            "calls": trace.get("calls") or [], "chat_calls": chat_calls(r),
            "dropped": [(d, DROPPED.get(d, d)) for d in trace.get("dropped") or []], "elapsed_ms": elapsed(r),
            "prompt": p, "completion": c, "n_calls": n, "cache_hit": cache_hit(r), "ticket": ticket,
            "sources": _sources(answer), "summary": answer.get("summary", "")}
