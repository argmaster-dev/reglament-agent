"""Model-proposed check questions for a document without a gold set (admin-p03).

The model sees a few points of the active version (the first substantive point of up to MAX_POINTS
different top-level sections) and proposes one employee-style question per point. Everything it
returns is untrusted: `validate` keeps only proposals whose expected point was offered AND exists in
the active version, drops empty/duplicate/long questions and caps the list. The expert accepts
or discards each proposal; accepted ones join the document's check questions (see checks.load_all).
"""
from __future__ import annotations

import asyncio
import re

from psycopg.rows import dict_row

from reglament_agent.admin import checks as C

MAX_POINTS = 5
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
        if point not in offered or point not in existing_points:
            continue
        key = C.norm(re.sub(r"[^\w\s]", "", q))
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append({"question": q, "expected_point": point})
        if len(kept) >= MAX_QUESTIONS:
            break
    return kept


async def _ask(messages: list[dict]) -> dict:
    result = await model_factory().chat_json(messages, SCHEMA, step="check_proposals",
                                             options={"num_predict": 900, "temperature": 0})
    return result.data


def propose(conn, doc_id: str, actor: str) -> tuple[list[dict], str | None]:
    """Ask the model and store valid proposals. Returns (stored proposals, message for the expert)."""
    version_no, points = active_points(conn, doc_id)
    if version_no is None:
        return [], "У документа нет активной версии: предлагать вопросы не по чему."
    picked = pick_points(points)
    if not picked:
        return [], "В активной версии нет достаточно содержательных пунктов для вопросов."
    try:
        data = asyncio.run(_ask(build_messages(picked)))
    except Exception as exc:  # noqa: BLE001 - model down, timeout, broken output: a message, never a 500
        return [], ("Модель сейчас недоступна или вернула некорректный ответ, вопросы не предложены. "
                    f"Попробуйте позже ({type(exc).__name__}).")
    taken = existing_questions(conn, doc_id)
    good = validate(data, {p["number"] for p in picked}, {p["number"] for p in points}, taken)
    if not good:
        return [], "Модель не предложила ни одного подходящего вопроса. Попробуйте ещё раз."
    conn.execute("DELETE FROM check_proposals WHERE doc_id=%s AND status='proposed'", (doc_id,))
    for g in good:
        conn.execute("INSERT INTO check_proposals (doc_id, version_no, question, expected_point, created_by) "
                     "VALUES (%s,%s,%s,%s,%s)", (doc_id, version_no, g["question"], g["expected_point"], actor))
    return list_proposals(conn, doc_id), None


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
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, version_no, question, expected_point FROM check_proposals "
        "WHERE doc_id=%s AND status='proposed' ORDER BY id", (doc_id,)).fetchall()
    _, points = active_points(conn, doc_id)
    live = {p["number"]: p["text"] for p in points}
    for r in rows:
        r["point_exists"] = r["expected_point"] in live
        r["point_text"] = live.get(r["expected_point"], "")
    return rows


def accept(conn, doc_id: str, proposal_id: int, actor: str) -> bool:
    _, points = active_points(conn, doc_id)
    row = conn.execute("SELECT expected_point FROM check_proposals WHERE id=%s AND doc_id=%s AND status='proposed'",
                       (proposal_id, doc_id)).fetchone()
    if row is None or row[0] not in {p["number"] for p in points}:
        return False
    conn.execute("UPDATE check_proposals SET status='accepted', decided_by=%s, decided_at=now() WHERE id=%s",
                 (actor, proposal_id))
    return True


def discard(conn, doc_id: str, proposal_id: int) -> None:
    conn.execute("DELETE FROM check_proposals WHERE id=%s AND doc_id=%s AND status='proposed'", (proposal_id, doc_id))


def accepted_questions(conn, doc_id: str) -> list[C.Question]:
    ver, _ = active_points(conn, doc_id)
    rows = conn.execute("SELECT question, expected_point, version_no FROM check_proposals "
                        "WHERE doc_id=%s AND status='accepted' ORDER BY id", (doc_id,)).fetchall()
    return [C.Question(q, p, "answer", None, v, None, "предложен моделью") for q, p, v in rows]
