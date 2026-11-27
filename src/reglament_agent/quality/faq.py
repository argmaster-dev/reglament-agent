"""Expert answers to tickets and the FAQ built from them.

A FAQ entry keeps its basis (document, point numbers, content hashes). When a later version of
the document changes or removes such a point the entry goes to `review` and is no longer found
(`recheck_faq_after_publication`). Private answers live only in `ticket_answers`: never indexed.
Search is fail-closed: an entry tied to a document is returned only while its `version_id` is the
document's active version, so a stale entry is hidden even before the re-check has run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reglament_agent.answer import SourceRef
from reglament_agent.dialog.lexicon import words, word_key
from reglament_agent.ingest.embedding import Embedder, vector_literal
from reglament_agent.storage.retrieve import FAQHit

from .mail import message_id_for

OUTSIDE_LABEL = "Ответ службы качества, в регламентах не закреплено"
BASIS_KINDS = ("points", "outside", "document")


class TicketError(ValueError):
    """Ticket missing or not open, or the answer is invalid."""


@dataclass(frozen=True)
class FaqRow:
    faq_id: int
    question: str
    answer: str
    basis_kind: str
    basis: list[dict]
    doc_id: str | None
    department: str | None
    score: float
    similarity: float
    doc_title: str | None = None
    version_no: int | None = None
    alt_questions: tuple[str, ...] = ()
    author_email: str = ""
    created_at: datetime | None = None


def basis_label(basis_kind: str, basis: list[dict]) -> str:
    if basis_kind == "outside":
        return OUTSIDE_LABEL
    if basis_kind == "document":
        docs = ", ".join(sorted({b.get("doc_title") or b["doc_id"] for b in basis}))
        return f"по документу {docs}"
    pts = ", ".join(f"п. {b['number']}" + (f" ({b['doc_title']})" if b.get("doc_title") else "")
                    for b in basis)
    return f"по {pts}"


def _lock_basis_documents(conn: Connection, doc_ids) -> None:
    # A concurrent version switch updates the document row. Hold shared locks until
    # the FAQ commit so publication either observes this entry or precedes its basis read.
    conn.execute("SELECT doc_id FROM documents WHERE doc_id=ANY(%s) ORDER BY doc_id FOR SHARE",
                 (sorted(set(doc_ids)),)).fetchall()


def _capture_basis(conn: Connection, kind: str, points: Sequence[tuple]) -> tuple[list[dict], str | None, int | None]:
    if kind not in BASIS_KINDS:
        raise TicketError(f"Неизвестный вид основания: {kind}")
    if kind == "outside":
        if points:
            raise TicketError("Для «вне регламентов» пункты не указываются.")
        return [], None, None
    if not points:
        raise TicketError("Укажите пункты регламентов или выберите «вне регламентов».")
    _lock_basis_documents(conn, (item[0] for item in points))
    cur = conn.cursor(row_factory=dict_row)
    basis: list[dict] = []
    for item in points:
        doc_id, number = item[0], item[1]
        column = item[2] if len(item) > 2 else None
        doc = cur.execute("SELECT doc_id, title, active_version_id FROM documents "
                          "WHERE doc_id=%s AND status='active' AND active_version_id IS NOT NULL",
                          (doc_id,)).fetchone()
        if doc is None:
            raise TicketError(f"Документ {doc_id} не найден или отключён.")
        if kind == "document":
            basis.append({"doc_id": doc_id, "doc_title": doc["title"], "number": None, "sha256": None,
                          "section_path": None, "column": None, "parent": None})
            continue
        p = cur.execute("SELECT number, parent, section_number, section_title, sha256 FROM points "
                        "WHERE version_id=%s AND number=%s", (doc["active_version_id"], number)).fetchone()
        if p is None:
            raise TicketError(f"Пункт {number} не найден в активной версии {doc_id}.")
        basis.append({"doc_id": doc_id, "doc_title": doc["title"], "number": p["number"],
                      "sha256": p["sha256"], "section_path": f"{p['section_number']} {p['section_title']}".strip(),
                      "column": column, "parent": p["parent"]})
    first = basis[0]["doc_id"]
    version = cur.execute("SELECT active_version_id FROM documents WHERE doc_id=%s", (first,)).fetchone()
    return basis, first, version["active_version_id"]


def _answer_mail(ticket: dict, answer: str, kind: str, basis: list[dict]) -> tuple[str, str]:
    subject = f"[Обращение №{ticket['number']}] Ответ службы качества"
    body = (f"Обращение №{ticket['number']}\n\nВаш вопрос:\n{ticket['question']}\n\n"
            f"Ответ службы качества:\n{answer}\n\nОснование: {basis_label(kind, basis)}\n")
    return subject, body


def answer_ticket(conn: Connection, *, number: str, expert_email: str, answer: str, basis_kind: str,
                  basis_points: Sequence[tuple], visibility: str, faq_question: str | None = None,
                  alt_questions: Sequence[str] = (), embedder: Embedder | None = None,
                  department: str | None = None, source: str = "cabinet", notify_mail: bool = True) -> dict:
    """Answer an open ticket in ONE transaction. Raises TicketError (e.g. already answered).

    `department` binds an «outside» FAQ entry to a department (None: visible to everyone).
    `source` is 'cabinet' or 'email'; `notify_mail=False` skips the outbox letter (the employee
    already received the expert's own reply as a copy).
    """
    answer = (answer or "").strip()
    if not answer:
        raise TicketError("Ответ не может быть пустым.")
    if visibility not in ("faq", "private"):
        raise TicketError("Видимость: faq или private.")
    alts = [a.strip() for a in alt_questions if a and a.strip()]
    question = (faq_question or "").strip()
    vector = None
    if visibility == "faq":
        if embedder is None:
            raise TicketError("Для публикации в FAQ нужен embedder.")
        if not question:
            raise TicketError("Укажите вопрос для FAQ.")
        # Embedding is computed before the transaction so no row lock is held during the call.
        vector = vector_literal(embedder.embed(["\n".join([question, *alts])])[0])
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        ticket = cur.execute("SELECT * FROM tickets WHERE number=%s FOR UPDATE", (number,)).fetchone()
        if ticket is None:
            raise TicketError(f"Обращение {number} не найдено.")
        if ticket["status"] != "open":
            raise TicketError(f"Обращение {number} уже обработано (статус: {ticket['status']}).")
        basis, doc_id, version_id = _capture_basis(conn, basis_kind, basis_points)
        faq_id = None
        if visibility == "faq":
            faq_id = cur.execute(
                "INSERT INTO faq_entries (question, alt_questions, answer, basis_kind, doc_id, version_id, "
                "basis, author_email, ticket_id, embedding, department) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) "
                "RETURNING id", (question, alts, answer, basis_kind, doc_id, version_id, Jsonb(basis),
                                 expert_email, ticket["id"], vector,
                                 department if basis_kind == "outside" else None)).fetchone()["id"]
        cur.execute("INSERT INTO ticket_answers (ticket_id, expert_email, answer, basis_kind, basis, "
                    "visibility, faq_id, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ticket["id"], expert_email, answer, basis_kind, Jsonb(basis), visibility, faq_id, source))
        cur.execute("UPDATE tickets SET status='answered', updated_at=now() WHERE id=%s", (ticket["id"],))
        if notify_mail:
            subject, body = _answer_mail(ticket, answer, basis_kind, basis)
            cur.execute("INSERT INTO mail_outbox (ticket_id, kind, to_addr, subject, body, message_id_header) "
                        "VALUES (%s,'expert_answer',%s,%s,%s,%s)",
                        (ticket["id"], ticket["user_email"], subject, body,
                         message_id_for(number, "expert_answer")))
        _notify_user(cur, ticket, answer, basis_kind, basis, expert_email, visibility)
    return {"number": number, "faq_id": faq_id, "basis_kind": basis_kind, "basis": basis,
            "visibility": visibility}


def _notify_user(cur, ticket: dict, answer: str, kind: str, basis: list[dict], expert_email: str,
                 visibility: str, updated: bool = False) -> None:
    payload = {"answer": answer, "basis_kind": kind, "basis": basis, "question": ticket["question"],
               "expert_email": expert_email, "visibility": visibility}
    if updated:
        payload["updated"] = True
    cur.execute("INSERT INTO chat_notifications (user_email, dialog_id, ticket_number, kind, payload) "
                "VALUES (%s,%s,%s,'answer',%s)",
                (ticket["user_email"], ticket["dialog_id"], ticket["number"], Jsonb(payload)))


def replace_ticket_answer(conn: Connection, *, number: str, expert_email: str, answer: str,
                          source: str = "email") -> dict:
    """«Ответ последний»: the newer expert answer replaces the stored one and its FAQ entry.

    The FAQ entry keeps its id, question, basis and status (a withdrawn entry stays withdrawn);
    the employee gets a fresh notification. Raises TicketError when there is nothing to replace.
    """
    answer = (answer or "").strip()
    if not answer:
        raise TicketError("Ответ не может быть пустым.")
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        ticket = cur.execute("SELECT * FROM tickets WHERE number=%s FOR UPDATE", (number,)).fetchone()
        if ticket is None:
            raise TicketError(f"Обращение {number} не найдено.")
        old = cur.execute("SELECT * FROM ticket_answers WHERE ticket_id=%s FOR UPDATE", (ticket["id"],)).fetchone()
        if old is None or ticket["status"] != "answered":
            raise TicketError(f"Обращение {number} ещё не отвечено.")
        cur.execute("UPDATE ticket_answers SET answer=%s, expert_email=%s, source=%s, updated_at=now() "
                    "WHERE id=%s", (answer, expert_email, source, old["id"]))
        if old["faq_id"] is not None:
            cur.execute("UPDATE faq_entries SET answer=%s, author_email=%s, updated_at=now() WHERE id=%s",
                        (answer, expert_email, old["faq_id"]))
        cur.execute("UPDATE tickets SET updated_at=now() WHERE id=%s", (ticket["id"],))
        _notify_user(cur, ticket, answer, old["basis_kind"], old["basis"], expert_email, old["visibility"],
                     updated=True)
    return {"number": number, "faq_id": old["faq_id"], "basis_kind": old["basis_kind"], "basis": old["basis"],
            "visibility": old["visibility"], "updated": True}


def close_ticket(conn: Connection, number: str, expert_email: str, kind: str, ref: str | None = None) -> None:
    """Close as duplicate of another ticket (`ref`) or as off-topic; the employee is told."""
    if kind not in ("duplicate", "offtopic"):
        raise TicketError("Вид закрытия: duplicate или offtopic.")
    ref = (ref or "").strip() or None
    if kind == "duplicate" and not ref:
        raise TicketError("Укажите номер обращения, дубликатом которого является это.")
    text = (f"Обращение закрыто как дубликат обращения №{ref}. Ответ по нему вы получите отдельно."
            if kind == "duplicate" else
            "Вопрос не относится к регламентам, поэтому обращение закрыто службой качества.")
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        t = cur.execute("SELECT * FROM tickets WHERE number=%s FOR UPDATE", (number,)).fetchone()
        if t is None:
            raise TicketError(f"Обращение {number} не найдено.")
        if t["status"] != "open":
            raise TicketError(f"Обращение {number} уже обработано (статус: {t['status']}).")
        cur.execute("UPDATE tickets SET status=%s, updated_at=now() WHERE id=%s",
                    ("closed_duplicate" if kind == "duplicate" else "closed_offtopic", t["id"]))
        cur.execute("INSERT INTO mail_outbox (ticket_id, kind, to_addr, subject, body, message_id_header) "
                    "VALUES (%s,'expert_answer',%s,%s,%s,%s)",
                    (t["id"], t["user_email"], f"[Обращение №{number}] Ответ службы качества",
                     f"Обращение №{number}\n\nВаш вопрос:\n{t['question']}\n\n{text}\n",
                     message_id_for(number, "expert_answer")))
        cur.execute("INSERT INTO chat_notifications (user_email, dialog_id, ticket_number, kind, payload) "
                    "VALUES (%s,%s,%s,'closed',%s)", (t["user_email"], t["dialog_id"], number,
                                                      Jsonb({"answer": text, "closed": kind, "ref": ref,
                                                             "expert_email": expert_email})))


def list_faq(conn: Connection, status: str | None = None) -> list[dict[str, Any]]:
    where, params = ("WHERE f.status=%s", (status,)) if status else ("WHERE f.status <> 'deleted'", ())
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT f.id, f.question, f.answer, f.basis_kind, f.doc_id, f.status, f.author_email, f.created_at, "
        "f.updated_at, t.number AS ticket_number FROM faq_entries f LEFT JOIN tickets t ON t.id=f.ticket_id "
        f"{where} ORDER BY f.id DESC", params).fetchall()


def get_faq(conn: Connection, faq_id: int) -> dict[str, Any] | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT f.id, f.question, f.alt_questions, f.answer, f.basis_kind, f.doc_id, f.version_id, f.basis, "
        "f.status, f.author_email, f.created_at, f.updated_at, f.reviewed_at, t.number AS ticket_number "
        "FROM faq_entries f LEFT JOIN tickets t ON t.id=f.ticket_id WHERE f.id=%s", (faq_id,)).fetchone()


def set_faq_status(conn: Connection, faq_id: int, status: str) -> bool:
    """`active` confirms an entry (re-pinning its basis to the current points), `deleted` removes it."""
    if status not in ("active", "review", "deleted"):
        raise ValueError("bad status")
    with conn.transaction():
        row = get_faq(conn, faq_id)
        if row is None:
            return False
        if status == "active" and row["basis_kind"] != "outside":
            _repin(conn, row)
        conn.execute("UPDATE faq_entries SET status=%s, updated_at=now(), "
                     "reviewed_at=CASE WHEN %s='active' THEN now() ELSE reviewed_at END WHERE id=%s",
                     (status, status, faq_id))
    return True


def _repin(conn: Connection, row: dict) -> None:
    """Expert confirmed a `review` entry: pin it to the current text of its points (removed ones block it)."""
    basis = [dict(b) for b in row["basis"]]
    _lock_basis_documents(conn, (b["doc_id"] for b in basis))
    cur = conn.cursor(row_factory=dict_row)
    for b in basis:
        if row["basis_kind"] == "document":
            continue
        st = point_state(conn, b)
        if st["state"] == "removed":
            raise TicketError(f"Пункт {b['number']} удалён из действующей версии: "
                              "исправьте ответ или удалите запись.")
        p = cur.execute("SELECT p.number, p.parent, p.sha256, p.section_number, p.section_title FROM points p "
                        "JOIN documents d ON d.active_version_id=p.version_id WHERE d.doc_id=%s AND p.number=%s",
                        (b["doc_id"], st["number"])).fetchone()
        b.update(number=p["number"], sha256=p["sha256"], parent=p["parent"],
                 section_path=f"{p['section_number']} {p['section_title']}".strip())
    version = conn.execute("SELECT active_version_id FROM documents WHERE doc_id=%s", (row["doc_id"],)).fetchone()
    conn.execute("UPDATE faq_entries SET basis=%s, version_id=%s WHERE id=%s",
                 (Jsonb(basis), version[0] if version else row["version_id"], row["id"]))


def point_state(conn: Connection, item: dict) -> dict:
    """Compare a stored basis point with the active version: state ok|renumbered|changed|removed."""
    cur = conn.cursor(row_factory=dict_row)
    doc = cur.execute("SELECT active_version_id FROM documents WHERE doc_id=%s AND status='active'",
                      (item["doc_id"],)).fetchone()
    if doc is None or doc["active_version_id"] is None:
        return {"state": "removed", "number": None, "text": None}
    vid = doc["active_version_id"]
    same = cur.execute("SELECT number, text, sha256 FROM points WHERE version_id=%s AND number=%s",
                       (vid, item["number"])).fetchone()
    if same is not None and same["sha256"] == item["sha256"]:
        return {"state": "ok", "number": same["number"], "text": same["text"]}
    moved = cur.execute("SELECT number, text FROM points WHERE version_id=%s AND sha256=%s AND parent IS NOT DISTINCT FROM %s "
                        "ORDER BY position LIMIT 1", (vid, item["sha256"], item.get("parent"))).fetchone()
    if moved is not None:
        return {"state": "renumbered", "number": moved["number"], "text": moved["text"]}
    if same is not None:
        return {"state": "changed", "number": same["number"], "text": same["text"]}
    return {"state": "removed", "number": None, "text": None}


def _check_entry(conn: Connection, entry: dict) -> tuple[str, list[dict]]:
    """Return ('ok'|'review', basis with renumbered points updated)."""
    basis = [dict(b) for b in entry["basis"]]
    if entry["basis_kind"] == "outside":
        return "ok", basis
    if entry["basis_kind"] == "document":
        for b in basis:
            row = conn.execute("SELECT active_version_id FROM documents WHERE doc_id=%s AND status='active'",
                               (b["doc_id"],)).fetchone()
            if row is None or row[0] != entry["version_id"]:
                return "review", basis
        return "ok", basis
    if not basis:
        return "review", basis
    for b in basis:
        st = point_state(conn, b)
        if st["state"] not in ("ok", "renumbered"):
            return "review", basis
        if st["state"] == "renumbered":
            b["number"] = st["number"]
    return "ok", basis


def review_reasons(conn: Connection, entry: dict) -> list[str]:
    """Why an entry cannot stay published, judged against the document state as it is now."""
    out = []
    for b in entry["basis"]:
        doc = conn.execute("SELECT status, title FROM documents WHERE doc_id=%s", (b["doc_id"],)).fetchone()
        title = (doc[1] if doc else None) or b.get("doc_title") or b["doc_id"]
        if doc is None or doc[0] != "active":
            out.append(f"документ «{title}» снят с публикации")
        elif entry["basis_kind"] == "document":
            out.append(f"документ «{title}» заменён другой версией")
        else:
            state = point_state(conn, b)["state"]
            if state == "removed":
                out.append(f"пункт {b['number']} удалён из действующей версии")
            elif state == "changed":
                out.append(f"текст пункта {b['number']} изменён (или строка таблицы перенесена)")
    return list(dict.fromkeys(out)) or ["основание записи изменилось"]


def record_review(conn: Connection, faq_id: int, actor: str, action: str, detail: dict | None = None) -> None:
    """One line of the check history: who, what, when and under which version of the document."""
    row = conn.execute("SELECT v.version_number FROM faq_entries f JOIN documents d ON d.doc_id=f.doc_id "
                       "JOIN document_versions v ON v.version_id=d.active_version_id WHERE f.id=%s",
                       (faq_id,)).fetchone()
    detail = {**(detail or {}), "version": row[0] if row else None}
    conn.execute("INSERT INTO faq_reviews (faq_id, actor, action, detail) VALUES (%s,%s,%s,%s)",
                 (faq_id, actor, action, Jsonb(detail)))


def recheck_faq_after_publication(conn: Connection, doc_id: str) -> int:
    """Move active entries whose basis changed in the new active version of `doc_id` to `review`.

    Returns the number of entries moved. Entries whose points are unchanged (same hash, same number
    or same parent after renumbering) stay active and are re-pinned to the new version.
    """
    moved = 0
    with conn.transaction():
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, basis_kind, doc_id, version_id, basis FROM faq_entries WHERE status='active' "
            "AND basis_kind <> 'outside' AND (doc_id=%s OR basis @> %s::jsonb) ORDER BY id FOR UPDATE",
            (doc_id, Jsonb([{"doc_id": doc_id}]))).fetchall()
        for row in rows:
            verdict, basis = _check_entry(conn, row)
            if verdict == "review":
                conn.execute("UPDATE faq_entries SET status='review', updated_at=now() WHERE id=%s", (row["id"],))
                record_review(conn, row["id"], "система", "to_review", {"reasons": review_reasons(conn, row)})
                moved += 1
            else:
                version = conn.execute("SELECT active_version_id FROM documents WHERE doc_id=%s",
                                       (row["doc_id"],)).fetchone()
                conn.execute("UPDATE faq_entries SET basis=%s, version_id=%s WHERE id=%s",
                             (Jsonb(basis), version[0] if version else row["version_id"], row["id"]))
    return moved


def search_rows(conn: Connection, question: str, vector: list[float], *, department: str | None = None,
                stage: str | None = None, limit: int = 3) -> list[FaqRow]:
    """Hybrid (vector + lexical) search over `active` entries only.

    Entries tied to a document must pin that document's active version (fail-closed); "outside"
    entries have no department and are visible everywhere. `PublishedFaqStore` adapts this to the
    `PublishedFAQ` protocol.
    """
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    rows = conn.cursor(row_factory=dict_row).execute("""
        WITH eligible AS (
            SELECT f.*, coalesce(d.department, f.department) AS dept, d.title AS dtitle, v.version_number AS vno
            FROM faq_entries f LEFT JOIN documents d ON d.doc_id = f.doc_id
            LEFT JOIN document_versions v ON v.version_id = f.version_id
            WHERE f.status = 'active'
              AND (f.basis_kind = 'outside'
                   OR (d.status = 'active' AND d.active_version_id = f.version_id))
              AND (%(dept)s::text IS NULL OR (f.basis_kind = 'outside' AND (f.department IS NULL OR f.department = %(dept)s))
                   OR d.department = %(dept)s)
              AND (%(stage)s::text IS NULL OR f.basis_kind = 'outside' OR %(stage)s = ANY(d.stages))),
        v AS (SELECT id, 1 - (embedding <=> %(vec)s::vector) AS sim,
                     row_number() OVER (ORDER BY embedding <=> %(vec)s::vector, id) AS r FROM eligible),
        l AS (SELECT e.id, row_number() OVER (ORDER BY ts_rank_cd(e.tsv, q.t) DESC, e.id) AS r
              FROM eligible e, (SELECT websearch_to_tsquery('russian', %(query)s) AS t) q WHERE e.tsv @@ q.t),
        fused AS (SELECT id, sum(1.0 / (60 + r)) AS score FROM
                  (SELECT id, r FROM v UNION ALL SELECT id, r FROM l) x GROUP BY id)
        SELECT e.id, e.question, e.alt_questions, e.answer, e.basis_kind, e.basis, e.doc_id, e.dept,
               fused.score, v.sim, e.dtitle, e.vno, e.author_email, e.created_at
        FROM fused JOIN eligible e ON e.id = fused.id JOIN v ON v.id = e.id
        ORDER BY fused.score DESC, e.id LIMIT %(limit)s""",
        {"vec": vector_literal(vector), "query": question, "dept": department, "stage": stage,
         "limit": limit}).fetchall()
    return [FaqRow(r["id"], r["question"], r["answer"], r["basis_kind"], r["basis"], r["doc_id"],
                   r["dept"], float(r["score"]), float(r["sim"]), r["dtitle"], r["vno"], tuple(r["alt_questions"]),
                   r["author_email"], r["created_at"]) for r in rows]


class PublishedFaqStore:
    """`storage.retrieve.PublishedFAQ` over `faq_entries`: active, current-basis entries only.

    FAQHit carries one basis reference: the first basis point (all points stay in the FAQ entry).
    """

    def search_published(self, conn: Connection, question: str, vector: list[float], *,
                         department: str | None, stage: str | None, limit: int) -> list[FAQHit]:
        hits = []
        for r in search_rows(conn, question, vector, department=department, stage=stage, limit=limit):
            doc = r.doc_id is not None
            point = next((b for b in r.basis if b.get("number")), None)
            basis = None
            if doc and point is not None:
                basis = SourceRef(r.doc_id, r.doc_title or r.doc_id, r.department or "", r.version_no or 0,
                                  point["number"], column=point.get("column"))
            source = SourceRef(r.doc_id or "faq", "FAQ службы качества", r.department or "Служба качества",
                               r.version_no or 0, "", faq_id=r.faq_id)
            stamp = f" от {r.created_at:%d.%m.%Y}" if r.created_at else ""
            attribution = f"Ответ службы качества{stamp} · {r.author_email} · {basis_label(r.basis_kind, r.basis)}"
            hits.append(FAQHit(source, r.answer, basis, r.department, stage if doc else None,
                               r.question, r.alt_questions, r.similarity, attribution))
        return hits


# A FAQ may answer a new wording only with strong embedding agreement AND shared content.
# Exact variants explicitly supplied by the expert take priority.
_FAQ_SIMILARITY = 0.78
_FAQ_MARGIN = 0.04
_FAQ_INTENTS = frozenset({"кто", "где", "когда", "сколько", "зачем", "почему", "какой", "какие"})
_FAQ_CONDITIONS = frozenset({"не", "нет", "нельзя", "без", "кроме"})


def _question_shape(tokens):
    intent = tokens[0] if tokens and tokens[0] in _FAQ_INTENTS else ""
    conditions = frozenset(t for t in tokens if t in _FAQ_CONDITIONS or t.isdigit())
    keys = {word_key(t) for t in tokens if len(t) >= 4 and t not in _FAQ_INTENTS}
    return intent, conditions, keys


def match_published(question: str, hits: list[FAQHit]) -> FAQHit | None:
    tokens = words(question)
    if not tokens:
        return None
    intent, conditions, keys = _question_shape(tokens)
    candidates = []
    for hit in hits:
        variants = [words(q) for q in (hit.question, *hit.alt_questions)]
        if tokens in variants:
            return hit
        if hit.similarity < _FAQ_SIMILARITY:
            continue
        for variant in variants:
            other_intent, other_conditions, other_keys = _question_shape(variant)
            if (intent, conditions) == (other_intent, other_conditions) and len(keys & other_keys) >= 2:
                candidates.append(hit)
                break
    candidates.sort(key=lambda hit: hit.similarity, reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[0].similarity - candidates[1].similarity < _FAQ_MARGIN):
        return None
    return candidates[0]
