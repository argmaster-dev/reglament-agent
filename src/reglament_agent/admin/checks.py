"""Check questions per regulation document: load, run through the agent, judge, store (admin-b04).

Questions live in `docx/checks/<doc_id>.toml` and are written by the quality service from the text of
the regulation. A run asks every question through the public `Answerer` contract on behalf of a
dedicated service identity and stores the verdicts in `check_runs`, so the page can compare the last
run with the previous one (before/after a version replacement).
"""
from __future__ import annotations

import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reglament_agent.answer import Answer, Outcome, UserContext

SERVICE_EMAIL = "quality-checks@service.local"
STALE_MINUTES = 30  # a «running» run older than this is shown as interrupted (server restart)


TICKET_OUTCOMES = {"no_basis", "external_doc", "conflict", "low_confidence"}  # the agent opens a ticket + mail


class CheckFileError(ValueError):
    """A check file cannot be used (shown to the expert as is)."""


@dataclass(frozen=True)
class Question:
    question: str
    point: str | None  # expected point number; None = no point of this document must be cited
    outcome: str  # expected Outcome value
    phrase: str | None = None  # must occur in the answer or in the cited point text
    version: int = 1  # document version the expected phrase was written against
    gold_id: str | None = None  # id of the approved gold case the question is taken from
    kind: str | None = None  # «перефраз» for a paraphrase of the gold question
    facts: tuple[str, ...] = ()  # further phrases that must also be present


@dataclass(frozen=True)
class CheckSet:
    doc_id: str
    department: str
    questions: list[Question]


def checks_dir() -> Path:
    if os.environ.get("CHECKS_DIR"):
        return Path(os.environ["CHECKS_DIR"])
    return Path(__file__).resolve().parents[3] / "docx" / "checks"


def load_questions(doc_id: str, directory: Path | None = None) -> CheckSet | None:
    path = (directory or checks_dir()) / f"{doc_id}.toml"
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CheckFileError(f"{path.name}: файл не читается ({exc})") from exc
    if data.get("doc_id", doc_id) != doc_id:
        raise CheckFileError(f"{path.name}: в файле указан документ «{data.get('doc_id')}», ожидался «{doc_id}»")
    if not str(data.get("department", "")).strip():
        raise CheckFileError(f"{path.name}: не указано подразделение (department)")
    known, seen, questions = {o.value for o in Outcome}, set(), []
    for i, q in enumerate(data.get("question", []), 1):
        text = str(q.get("question", "")).strip()
        if not text:
            raise CheckFileError(f"{path.name}: вопрос №{i} пустой")
        if norm(text) in seen:
            raise CheckFileError(f"{path.name}: вопрос «{text}» повторяется")
        seen.add(norm(text))
        outcome = q.get("outcome", q.get("expected_outcome", "answer"))
        if outcome in TICKET_OUTCOMES:
            raise CheckFileError(f"{path.name}: вопрос «{text}» ожидает исход «{outcome}», а он создаёт "
                                 "обращение и письмо службе качества; проверочные вопросы не должны вести к обращению")
        if outcome not in known:
            raise CheckFileError(f"{path.name}: вопрос «{text}»: неизвестный исход «{outcome}»")
        questions.append(Question(text, q.get("point"), outcome, q.get("phrase"), int(q.get("version", 1)),
                                  q.get("gold_id"), q.get("kind"), tuple(q.get("facts", ()))))
    if not questions:
        raise CheckFileError(f"{path.name}: нет ни одного вопроса")
    return CheckSet(doc_id, data["department"].strip(), questions)


def load_all(doc_id: str) -> CheckSet | None:
    """Questions of the gold file plus the proposals the expert accepted (admin-p03)."""
    from reglament_agent.admin import check_proposals as P
    from reglament_agent.admin import jobs

    cs = load_questions(doc_id)
    with jobs.connect() as conn:
        extra = P.accepted_questions(conn, doc_id)
        dept = conn.execute("SELECT department FROM documents WHERE doc_id=%s", (doc_id,)).fetchone()
    if not extra:
        return cs
    known = {norm(q.question) for q in cs.questions} if cs else set()
    extra = [q for q in extra if norm(q.question) not in known]
    if cs is None:
        return CheckSet(doc_id, (dept[0] if dept else "").strip(), extra)
    return CheckSet(doc_id, cs.department, [*cs.questions, *extra])


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


# --- judging -----------------------------------------------------------------------------------

def _statements(answer: Answer):
    yield from answer.statements
    for variant in answer.variants:
        yield from variant.statements


def _point_text(conn, doc_id: str, number: str, version_no: int) -> str | None:
    row = conn.execute("SELECT p.text FROM points p JOIN document_versions v USING (version_id) "
                       "WHERE v.doc_id=%s AND v.version_number=%s AND p.number=%s",
                       (doc_id, version_no, number)).fetchone()
    return row[0] if row else None


def _active_version(conn, doc_id: str) -> int | None:
    row = conn.execute("SELECT v.version_number FROM documents d JOIN document_versions v "
                       "ON v.version_id=d.active_version_id WHERE d.doc_id=%s AND d.status='active'",
                       (doc_id,)).fetchone()
    return row[0] if row else None


def evaluate(conn, doc_id: str, q: Question, answer: Answer, seconds: float, error: str | None = None) -> dict:
    active = _active_version(conn, doc_id)
    refs = [(src.point, src.version_no) for st in _statements(answer) for src in st.sources
            if src.faq_id is None and src.doc_id == doc_id]
    found = list(dict.fromkeys(point for point, _ in refs))
    current = [(p, v) for p, v in refs if v == active]
    stale = [(p, v) for p, v in refs if v != active]
    versions = dict.fromkeys(v for p, v in current if p == q.point)
    version_no = next(iter(versions), (current or refs or [(None, None)])[0][1])
    reason = None
    if q.point and any(p == q.point for p, _ in stale) and not any(p == q.point for p, _ in current):
        old = next(v for p, v in stale if p == q.point)
        reason = f"ссылка на устаревшую версию {old}, активна {active if active else 'нет'}"
        version_no = old
    elif q.point is None and stale:
        reason = f"ссылка на устаревшую версию {stale[0][1]}, активна {active if active else 'нет'}"
    texts = [answer.summary, *(st.text for st in _statements(answer))]
    wanted = [x for x in (q.phrase, *q.facts) if x]
    cited_ok = bool(q.point) and any(p == q.point for p, _ in current)
    phrase_found = None
    if wanted:
        hay = [norm(t) for t in texts]
        if cited_ok and version_no is not None:
            hay.append(norm(_point_text(conn, doc_id, q.point, version_no) or ""))
        phrase_found = cited_ok and all(any(norm(w) in h for h in hay) for w in wanted)
    point_ok = cited_ok if q.point else not refs
    outcome = str(answer.outcome.value if isinstance(answer.outcome, Outcome) else answer.outcome)
    outcome_ok = outcome == q.outcome
    passed = bool(point_ok and outcome_ok and phrase_found is not False and error is None)
    changed = bool(not passed and error is None and cited_ok and outcome_ok and phrase_found is False
                   and version_no is not None and version_no > q.version)
    excerpt = None
    if changed:
        cited = _point_text(conn, doc_id, q.point, version_no) or " ".join(texts)
        excerpt = norm(cited)[:160] + ("…" if len(norm(cited)) > 160 else "")
    return {"question": q.question, "expected_point": q.point, "expected_outcome": q.outcome,
            "expected_phrase": q.phrase, "found_points": found, "outcome": outcome, "version_no": version_no,
            "phrase_found": phrase_found, "point_ok": point_ok, "outcome_ok": outcome_ok,
            "passed": passed, "status": "passed" if passed else "changed" if changed else "failed",
            "changed_excerpt": excerpt, "expected_version": q.version, "reason": reason, "gold_id": q.gold_id,
            "kind": q.kind, "seconds": round(seconds, 2), "answer_text": " ".join(t for t in texts if t),
            "error": error}


# --- storage -----------------------------------------------------------------------------------

def start_run(doc_id: str, actor: str) -> int:
    from reglament_agent.admin import jobs

    cs = load_all(doc_id)
    with jobs.connect() as conn:
        active = conn.execute("SELECT v.version_number FROM documents d JOIN document_versions v "
                              "ON v.version_id=d.active_version_id WHERE d.doc_id=%s", (doc_id,)).fetchone()
        return conn.execute("INSERT INTO check_runs (doc_id, version_no, started_by, total) VALUES (%s,%s,%s,%s) "
                            "RETURNING id", (doc_id, active[0] if active else None, actor,
                                             len(cs.questions) if cs else 0)).fetchone()[0]


def last_runs(conn, doc_id: str, limit: int = 2) -> list[dict]:
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT *, status='running' AND started_at < now() - make_interval(mins => %s) AS stale "
        "FROM check_runs WHERE doc_id=%s ORDER BY id DESC LIMIT %s", (STALE_MINUTES, doc_id, limit)).fetchall()
    for r in rows:
        if r.pop("stale"):
            r["status"], r["error"] = "failed", r["error"] or "Прервано перезапуском сервера"
    return rows


def running_run(conn, doc_id: str) -> int | None:
    runs = last_runs(conn, doc_id, 1)
    return runs[0]["id"] if runs and runs[0]["status"] == "running" else None


def _store(run_id: int, results: list[dict], status: str | None = None, error: str | None = None) -> None:
    from reglament_agent.admin import jobs

    with jobs.connect() as conn:
        conn.execute("UPDATE check_runs SET results=%s, status=coalesce(%s,status), error=coalesce(%s,error), "
                     "finished_at=CASE WHEN %s::text IS NULL THEN finished_at ELSE now() END WHERE id=%s",
                     (Jsonb(results), status, error, status, run_id))


# --- running -----------------------------------------------------------------------------------

def services_factory():
    """The same services the chat uses (CHAT_SERVICES_FACTORY / database); replaced in tests."""
    from reglament_agent.chat.services import build_services

    return build_services()


async def _noop(_: str) -> None:
    return None


async def execute_run(run_id: int, services=None) -> None:
    """Ask every question sequentially; progress is visible after each one. Never raises."""
    import asyncio

    from reglament_agent.admin import jobs

    results: list[dict] = []
    try:
        with jobs.connect() as conn:
            doc_id = conn.execute("SELECT doc_id FROM check_runs WHERE id=%s", (run_id,)).fetchone()[0]
        cs = load_all(doc_id)
        services = services or services_factory()
        await services.profiles.set_department(SERVICE_EMAIL, cs.department)
        for i, q in enumerate(cs.questions):
            user = UserContext(SERVICE_EMAIL, "expert", f"checks-{run_id}-{i}", f"checks-{run_id}-{i}")
            started, error = time.monotonic(), None
            try:
                answer = await services.answerer.answer(user, q.question, _noop)
            except Exception as exc:  # noqa: BLE001 - recorded as a failed question, the run goes on
                answer, error = Answer(Outcome.ERROR, "Агент не ответил."), f"{type(exc).__name__}: {exc}"
            seconds = time.monotonic() - started

            def judge(answer=answer, q=q, seconds=seconds, error=error):
                with jobs.connect() as conn:
                    return evaluate(conn, doc_id, q, answer, seconds, error)

            results.append(await asyncio.to_thread(judge))
            await asyncio.to_thread(_store, run_id, results)
        await asyncio.to_thread(_store, run_id, results, "done")
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(_store, run_id, results, "failed", f"{type(exc).__name__}: {exc}")


def spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="checks-run").start()


def launch(run_id: int) -> None:
    import asyncio

    spawn(lambda: asyncio.run(execute_run(run_id)))
