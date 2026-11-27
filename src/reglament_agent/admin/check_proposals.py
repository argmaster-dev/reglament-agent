"""Model-proposed check questions for a document without a gold set (admin-p03).

The model sees a few points of the active version (the first substantive point of up to MAX_POINTS
different top-level sections) and proposes one employee-style question per point. Everything it
returns is untrusted: `validate` keeps only proposals whose expected point was offered AND exists in
the active version, drops empty/duplicate/long questions and caps the list. The expert accepts
or discards each proposal; accepted ones join the document's check questions (see checks.load_all).
"""
from __future__ import annotations

import re
from collections.abc import Callable

from psycopg.rows import dict_row

from reglament_agent.admin import checks as C
from reglament_agent.model.queue import GenerationQueueFull

MAX_POINTS = 5
MIN_QUESTIONS = 3
MAX_QUESTIONS = 5
MAX_QUESTION_LEN = 300
MIN_POINT_LEN = 40
POINT_PROMPT_LEN = 700

SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "maxItems": MAX_QUESTIONS, "items": {
        "type": "object",
        "properties": {"question": {"type": "string"}, "expected_point": {"type": "string"}},
        "required": ["question", "expected_point"]}}},
    "required": ["questions"],
}

SYSTEM = ("Ты помогаешь службе качества проверять ассистента по внутренним регламентам. "
          "Тебе даны пункты одного регламента с номерами. Для КАЖДОГО пункта придумай один вопрос, "
          "который задал бы рядовой сотрудник своими словами, не копируя текст пункта. "
          "Вопрос должен находить ответ только в этом пункте: не используй сведения из других пунктов "
          "и из общих знаний. В поле expected_point укажи номер пункта ровно так, как он указан в списке. "
          "Не придумывай номеров, которых нет в списке. Один вопрос на пункт, без повторов.")


class ProposalError(Exception):
    """Shown to the expert as is."""


def model_factory():
    """The model client; replaced in tests."""
    from reglament_agent.model.ollama import OllamaClient

    return OllamaClient()


def active_points(conn, doc_id: str) -> tuple[int | None, list[dict]]:
    row = conn.execute("SELECT v.version_id, v.version_number FROM documents d JOIN document_versions v "
                       "ON v.version_id=d.active_version_id WHERE d.doc_id=%s AND d.status='active'",
                       (doc_id,)).fetchone()
    if row is None:
        return None, []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT number, text, position FROM points WHERE version_id=%s ORDER BY position", (row[0],)).fetchall()
    return row[1], rows


def pick_points(points: list[dict]) -> list[dict]:
    """First substantive point of each top-level section; at most MAX_POINTS sections, spread evenly."""
    firsts: dict[str, dict] = {}
    for p in points:  # already ordered by position
        top = p["number"].split(".")[0]
        if top not in firsts and len(C.norm(p["text"])) >= MIN_POINT_LEN:
            firsts[top] = p
    chosen = list(firsts.values())
    n = len(chosen)
    if n <= MAX_POINTS:
        return chosen
    return [chosen[round(i * (n - 1) / (MAX_POINTS - 1))] for i in range(MAX_POINTS)]


def build_messages(picked: list[dict]) -> list[dict]:
    listing = "\n\n".join(f"Пункт {p['number']}: {C.norm(p['text'])[:POINT_PROMPT_LEN]}" for p in picked)
    user = (f"Пункты регламента:\n\n{listing}\n\nПредложи по одному вопросу на каждый пункт (не больше "
            f"{MAX_QUESTIONS}). Вопрос должен быть отвечаем только по самому пункту. "
            'Ответ верни JSON: {"questions": [{"question": "...", "expected_point": "номер пункта"}]}.')
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def validate(data, offered: set[str], existing_points: set[str], taken: set[str] = frozenset()) -> list[dict]:
    """The safety net: keep only well-formed proposals about offered, existing points."""
    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    seen, kept, used_points = set(taken), [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        q, point = item.get("question"), item.get("expected_point")
        if not isinstance(q, str) or not isinstance(point, str):
            continue
        q, point = q.strip(), point.strip()
        if not q or len(q) > MAX_QUESTION_LEN:
            continue
        if point not in offered or point not in existing_points or point in used_points:
            continue
        key = C.norm(re.sub(r"[^\w\s]", "", q))
        if not key or key in seen:
            continue
        seen.add(key)
        used_points.add(point)
        kept.append({"question": q, "expected_point": point})
        if len(kept) >= MAX_QUESTIONS:
            break
    return kept


def propose(conn, doc_id: str, actor: str, ask: Callable[[list[dict]], dict]) -> tuple[list[dict], str | None]:
    """Ask the model and store valid proposals. Returns (stored proposals, message for the expert)."""
    version_no, points = active_points(conn, doc_id)
    if version_no is None:
        return [], "У документа нет активной версии: предлагать вопросы не по чему."
    picked = pick_points(points)
    if not picked:
        return [], "В активной версии нет достаточно содержательных пунктов для вопросов."
    try:
        data = ask(build_messages(picked))
    except GenerationQueueFull:
        return [], "Очередь запросов к модели заполнена. Попробуйте предложить вопросы позже."
    except Exception as exc:  # noqa: BLE001 - model down, timeout, broken output: a message, never a 500
        return [], ("Модель сейчас недоступна или вернула некорректный ответ, вопросы не предложены. "
                    f"Попробуйте позже ({type(exc).__name__}).")
    with conn.transaction():
        live = locked_version(conn, doc_id)
        if live is None or live[1] != version_no:
            return [], "Активная версия документа изменилась. Предложите вопросы заново."
        offered = {p["number"] for p in picked}
        good = validate(data, offered, {p["number"] for p in points}, existing_questions(conn, doc_id))
        if not good:
            return [], "Модель не предложила ни одного подходящего вопроса. Попробуйте ещё раз."
        conn.execute("DELETE FROM check_proposals WHERE doc_id=%s AND status='proposed'", (doc_id,))
        for g in good:
            conn.execute("INSERT INTO check_proposals (doc_id, version_no, question, expected_point, created_by) "
                         "VALUES (%s,%s,%s,%s,%s)", (doc_id, version_no, g["question"], g["expected_point"], actor))
        minimum = min(MIN_QUESTIONS, len(offered))
        message = None
        if len(good) < minimum:
            message = (f"Получен неполный набор: охвачено разделов — {len(good)} из {minimum} необходимых. "
                       "Можно проверить эти вопросы или запросить новый набор.")
        return list_proposals(conn, doc_id), message


def locked_version(conn, doc_id: str):
    """Keep the document on this version during a short write transaction, after model inference."""
    row = conn.execute("SELECT active_version_id FROM documents WHERE doc_id=%s AND status='active' "
                       "FOR UPDATE", (doc_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return conn.execute("SELECT version_id, version_number FROM document_versions WHERE version_id=%s",
                        (row[0],)).fetchone()


def existing_questions(conn, doc_id: str) -> set[str]:
    try:
        cs = C.load_questions(doc_id)
    except C.CheckFileError:
        cs = None
    texts = [q.question for q in cs.questions] if cs else []
    texts += [r[0] for r in conn.execute("SELECT question FROM check_proposals WHERE doc_id=%s AND status='accepted'",
                                         (doc_id,)).fetchall()]
    return {C.norm(re.sub(r"[^\w\s]", "", t)) for t in texts}


def list_proposals(conn, doc_id: str) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT c.id, c.version_no, c.question, c.expected_point, p.text AS point_text, "
        "p.point_id IS NOT NULL AS point_exists FROM check_proposals c "
        "JOIN documents d ON d.doc_id=c.doc_id AND d.status='active' "
        "JOIN document_versions v ON v.version_id=d.active_version_id AND v.version_number=c.version_no "
        "LEFT JOIN points p ON p.version_id=v.version_id AND p.number=c.expected_point "
        "WHERE c.doc_id=%s AND c.status='proposed' ORDER BY c.id", (doc_id,)).fetchall()


def accept(conn, doc_id: str, proposal_id: int, actor: str) -> bool:
    with conn.transaction():
        version = locked_version(conn, doc_id)
        if version is None:
            return False
        version_id, version_no = version
        return conn.execute(
            "UPDATE check_proposals c SET status='accepted', decided_by=%s, decided_at=now() "
            "WHERE c.id=%s AND c.doc_id=%s AND c.status='proposed' AND c.version_no=%s "
            "AND EXISTS (SELECT 1 FROM points p WHERE p.version_id=%s AND p.number=c.expected_point)",
            (actor, proposal_id, doc_id, version_no, version_id),
        ).rowcount == 1


def discard(conn, doc_id: str, proposal_id: int) -> None:
    conn.execute("DELETE FROM check_proposals WHERE id=%s AND doc_id=%s AND status='proposed'", (proposal_id, doc_id))


def accepted_questions(conn, doc_id: str) -> list[C.Question]:
    ver, _ = active_points(conn, doc_id)
    rows = conn.execute("SELECT question, expected_point, version_no FROM check_proposals "
                        "WHERE doc_id=%s AND status='accepted' ORDER BY id", (doc_id,)).fetchall()
    return [C.Question(q, p, "answer", None, v, None, "предложен моделью") for q, p, v in rows]
