"""Document versions: diff, withdraw, rollback and expert review of FAQ entries after a change.

Everything that changes the active version runs in one transaction under the same advisory lock as
publication. The consequences shown before a confirmation are computed by performing the very same
change inside a transaction that is rolled back, so the preview cannot disagree with the result.
"""
from __future__ import annotations

import difflib
import re
from typing import Callable

from markupsafe import Markup, escape
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reglament_agent.ingest.text import content_hash
from reglament_agent.quality import faq as F

ACTIONS = {"to_review": "отправлено на проверку", "auto_restore": "возвращено автоматически",
           "confirm": "подтверждено", "edit": "ответ исправлен", "repin": "перепривязано",
           "delete": "удалено"}


class VersionError(ValueError):
    """The requested operation is not possible (shown to the expert as is)."""


# --- diff --------------------------------------------------------------------------------------

def load_points(conn: Connection, version_id: int) -> dict[str, dict]:
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT number, parent, sha256, text, position FROM points WHERE version_id=%s ORDER BY position",
        (version_id,)).fetchall()
    return {r["number"]: r for r in rows}


def parsed_points(parsed) -> dict[str, dict]:
    return {p.number: {"number": p.number, "parent": p.parent, "sha256": content_hash(p.text),
                       "text": p.text, "position": p.position} for p in parsed.points}


def word_diff(old: str, new: str) -> tuple[Markup, Markup]:
    """Word-level diff as safe HTML: removed words in <del> on the left, added in <ins> on the right."""
    a, b = re.findall(r"\w+|\s+|[^\w\s]", old), re.findall(r"\w+|\s+|[^\w\s]", new)
    left, right = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        x, y = "".join(a[i1:i2]), "".join(b[j1:j2])
        if tag == "equal":
            left.append(escape(x))
            right.append(escape(y))
            continue
        if x:
            left.append(Markup("<del>{}</del>").format(x))
        if y:
            right.append(Markup("<ins>{}</ins>").format(y))
    return Markup("").join(left), Markup("").join(right)


def diff_points(old: dict[str, dict], new: dict[str, dict]) -> dict:
    """Added / removed / changed / renumbered points. Renumbering = same text, same parent."""
    same, rest_old, rest_new = 0, dict(old), dict(new)
    for n in [n for n in old if n in new and old[n]["sha256"] == new[n]["sha256"]]:
        same += 1
        rest_old.pop(n)
        rest_new.pop(n)
    renumbered = []
    for n_old, o in sorted(rest_old.items(), key=lambda kv: kv[1]["position"]):
        match = next((n for n, c in sorted(rest_new.items(), key=lambda kv: kv[1]["position"])
                      if c["sha256"] == o["sha256"] and c["parent"] == o["parent"]), None)
        if match is not None:
            renumbered.append({"old": n_old, "new": match, "text": o["text"]})
            rest_old.pop(n_old)
            rest_new.pop(match)
    changed = []
    for n in sorted((n for n in rest_old if n in rest_new), key=lambda n: rest_new[n]["position"]):
        left, right = word_diff(rest_old[n]["text"], rest_new[n]["text"])
        changed.append({"number": n, "old_html": left, "new_html": right})
        rest_old.pop(n)
        rest_new.pop(n)
    by_pos = lambda d: [{"number": n, "text": p["text"]} for n, p in  # noqa: E731
                        sorted(d.items(), key=lambda kv: kv[1]["position"])]
    return {"added": by_pos(rest_new), "removed": by_pos(rest_old), "changed": changed,
            "renumbered": renumbered, "same": same,
            "total": len(rest_new) + len(changed) + len(renumbered)}


# --- documents and versions --------------------------------------------------------------------

def get_document(conn: Connection, doc_id: str) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT doc_id, title, department, status, active_version_id FROM documents WHERE doc_id=%s",
        (doc_id,)).fetchone()


def list_versions(conn: Connection, doc_id: str) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT v.version_id, v.version_number AS number, v.created_at, v.uploaded_by, v.filename, "
        "(SELECT count(*) FROM points p WHERE p.version_id=v.version_id) AS points, "
        "(v.version_id = d.active_version_id) AS active "
        "FROM document_versions v JOIN documents d ON d.doc_id=v.doc_id WHERE v.doc_id=%s "
        "ORDER BY v.version_number DESC", (doc_id,)).fetchall()


def version_id_of(conn: Connection, doc_id: str, number: int) -> int:
    row = conn.execute("SELECT version_id FROM document_versions WHERE doc_id=%s AND version_number=%s",
                       (doc_id, number)).fetchone()
    if row is None:
        raise VersionError(f"Версия {number} не найдена.")
    return row[0]


def diff_versions(conn: Connection, doc_id: str, old_number: int, new_number: int) -> dict:
    return diff_points(load_points(conn, version_id_of(conn, doc_id, old_number)),
                       load_points(conn, version_id_of(conn, doc_id, new_number)))


# --- FAQ impact --------------------------------------------------------------------------------

_LINKED = "(doc_id=%s OR basis @> %s::jsonb)"


def _linked(doc_id: str) -> tuple:
    return doc_id, Jsonb([{"doc_id": doc_id}])


def _reasons(conn: Connection, entry: dict) -> list[str]:
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
            state = F.point_state(conn, b)["state"]
            if state == "removed":
                out.append(f"пункт {b['number']} удалён из действующей версии")
            elif state == "changed":
                out.append(f"текст пункта {b['number']} изменён (или строка таблицы перенесена)")
    return list(dict.fromkeys(out)) or ["основание записи изменилось"]


def _restore_reviewed(conn: Connection, doc_id: str, actor: str) -> list[int]:
    """Entries sent to review only because their version stopped being active come back with it."""
    restored = []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT f.id, f.basis_kind, f.doc_id, f.version_id, f.basis FROM faq_entries f "
        "JOIN documents d ON d.doc_id=f.doc_id WHERE f.status='review' AND f.basis_kind <> 'outside' "
        "AND f.version_id=d.active_version_id AND (f.doc_id=%s OR f.basis @> %s::jsonb) ORDER BY f.id",
        _linked(doc_id)).fetchall()
    for row in rows:
        verdict, basis = F._check_entry(conn, row)
        if verdict == "ok":
            conn.execute("UPDATE faq_entries SET status='active', basis=%s, updated_at=now() WHERE id=%s",
                         (Jsonb(basis), row["id"]))
            log(conn, row["id"], actor, "auto_restore", {"reason": "версия документа снова действующая"})
            restored.append(row["id"])
    return restored


def _apply(conn: Connection, doc_id: str, actor: str, mutate: Callable[[Connection], None]) -> dict:
    """Run `mutate`, then the FAQ re-check. Returns entries sent to review (with reasons) and restored."""
    cur = conn.cursor(row_factory=dict_row)
    before = {r["id"] for r in cur.execute(
        "SELECT id FROM faq_entries WHERE status='active' AND basis_kind <> 'outside' AND " + _LINKED,
        _linked(doc_id)).fetchall()}
    mutate(conn)
    F.recheck_faq_after_publication(conn, doc_id)
    moved = []
    for r in cur.execute("SELECT id, question, basis_kind, basis FROM faq_entries WHERE id=ANY(%s) "
                         "AND status='review' ORDER BY id", (list(before),)).fetchall():
        reasons = _reasons(conn, r)
        log(conn, r["id"], actor, "to_review", {"reasons": reasons})
        moved.append({"id": r["id"], "question": r["question"], "reasons": reasons})
    restored_ids = _restore_reviewed(conn, doc_id, actor)
    restored = [{"id": r["id"], "question": r["question"]} for r in cur.execute(
        "SELECT id, question FROM faq_entries WHERE id=ANY(%s) ORDER BY id", (restored_ids,)).fetchall()]
    return {"review": moved, "restored": restored}


def _lock(conn: Connection, doc_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7))", (doc_id,))


def _set_active(doc_id: str, version_id: int | None, status: str):
    def mutate(conn):
        if version_id is None:
            conn.execute("UPDATE documents SET status=%s WHERE doc_id=%s", (status, doc_id))
        else:
            conn.execute("UPDATE documents SET active_version_id=%s, status=%s WHERE doc_id=%s",
                         (version_id, status, doc_id))
    return mutate


def _check_switch(conn: Connection, doc_id: str, version_id: int | None) -> dict:
    doc = get_document(conn, doc_id)
    if doc is None:
        raise VersionError("Документ не найден.")
    if version_id is None:
        if doc["status"] != "active":
            raise VersionError("Документ уже снят с публикации.")
    elif doc["status"] == "active" and doc["active_version_id"] == version_id:
        raise VersionError("Эта версия уже действующая.")
    return doc


def preview_withdraw(conn: Connection, doc_id: str) -> dict:
    with conn.transaction(force_rollback=True):
        _check_switch(conn, doc_id, None)
        return _apply(conn, doc_id, "preview", _set_active(doc_id, None, "inactive"))


def preview_activate(conn: Connection, doc_id: str, version_id: int) -> dict:
    with conn.transaction(force_rollback=True):
        _check_switch(conn, doc_id, version_id)
        return _apply(conn, doc_id, "preview", _set_active(doc_id, version_id, "active"))


def withdraw(conn: Connection, doc_id: str, actor: str) -> dict:
    with conn.transaction():
        _lock(conn, doc_id)
        _check_switch(conn, doc_id, None)
        return _apply(conn, doc_id, actor, _set_active(doc_id, None, "inactive"))


def activate_version(conn: Connection, doc_id: str, version_id: int, actor: str) -> dict:
    """Rollback to an earlier version (or put a withdrawn document back): one transaction."""
    with conn.transaction():
        _lock(conn, doc_id)
        _check_switch(conn, doc_id, version_id)
        return _apply(conn, doc_id, actor, _set_active(doc_id, version_id, "active"))


def preview_replacement(conn: Connection, doc_id: str, parsed, digest: str) -> dict:
    """Consequences of publishing `parsed` over the active version, without publishing anything."""
    with conn.transaction(force_rollback=True):
        doc = get_document(conn, doc_id)
        if doc is None or doc["active_version_id"] is None:
            return {"review": [], "restored": []}
        number = conn.execute("SELECT coalesce(max(version_number),0)+1 FROM document_versions "
                              "WHERE doc_id=%s", (doc_id,)).fetchone()[0]
        vid = conn.execute(
            "INSERT INTO document_versions(doc_id,version_number,sha256,filename,uploaded_by,stats,source_docx) "
            "VALUES (%s,%s,%s,'preview','preview','{}'::jsonb,''::bytea) RETURNING version_id",
            (doc_id, number, "preview-" + digest)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO points (version_id,doc_id,number,level,parent,section_number,section_title,"
                "heading,text,blocks,sentences,position,sha256,embedding) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,'[]'::jsonb,'{}',%s,%s,array_fill(0.1::real, ARRAY[1024])::vector)",
                [(vid, doc_id, p.number, p.level, p.parent, p.section_number, p.section_title, p.heading,
                  p.text, p.position, content_hash(p.text)) for p in parsed.points])
        return _apply(conn, doc_id, "preview", _set_active(doc_id, vid, "active"))


# --- FAQ review --------------------------------------------------------------------------------

def log(conn: Connection, faq_id: int, actor: str, action: str, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO faq_reviews (faq_id, actor, action, detail) VALUES (%s,%s,%s,%s)",
                 (faq_id, actor, action, Jsonb(detail or {})))


def history(conn: Connection, faq_id: int) -> list[dict]:
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT actor, action, detail, created_at FROM faq_reviews WHERE faq_id=%s ORDER BY id", (faq_id,)
    ).fetchall()
    for r in rows:
        r["label"] = ACTIONS.get(r["action"], r["action"])
    return rows


def _missing_cause(conn: Connection, entry: dict, rows: list[dict]) -> dict | None:
    """Publishing a replacement keeps no history of its own: derive the cause of the review now."""
    if entry["status"] != "review" or any(r["action"] == "to_review" and r["created_at"] >= entry["updated_at"]
                                          for r in rows):
        return None
    return {"reasons": _reasons(conn, entry)}


def _backfill(conn: Connection, entry: dict) -> None:
    """Before an expert's decision, persist why the entry was under review."""
    cause = _missing_cause(conn, entry, history(conn, entry["id"]))
    if cause:
        conn.execute("INSERT INTO faq_reviews (faq_id, actor, action, detail, created_at) "
                     "VALUES (%s,'система','to_review',%s,%s)", (entry["id"], Jsonb(cause), entry["updated_at"]))


def review_context(conn: Connection, faq_id: int) -> dict | None:
    entry = F.get_faq(conn, faq_id)
    if entry is None:
        return None
    cur = conn.cursor(row_factory=dict_row)
    items = []
    for b in entry["basis"]:
        item = {**b, "old_html": None, "new_html": None, "old_text": None, "new_text": None, "note": None}
        if b.get("number"):
            old = cur.execute("SELECT text FROM points WHERE doc_id=%s AND number=%s AND sha256=%s "
                              "ORDER BY version_id LIMIT 1", (b["doc_id"], b["number"], b["sha256"])).fetchone()
            st = F.point_state(conn, b)
            item["old_text"] = old["text"] if old else None
            item["new_text"] = st["text"]
            item["new_number"] = st["number"]
            item["state"] = st["state"]
            if item["old_text"] is not None and item["new_text"] is not None:
                item["old_html"], item["new_html"] = word_diff(item["old_text"], item["new_text"])
            if st["state"] == "removed":
                doc = get_document(conn, b["doc_id"])
                item["note"] = ("документ снят с публикации" if doc and doc["status"] != "active"
                                else "пункта нет в действующей версии")
        items.append(item)
    docs = cur.execute("SELECT doc_id, title FROM documents WHERE status='active' AND "
                       "active_version_id IS NOT NULL ORDER BY title").fetchall()
    rows = history(conn, faq_id)
    cause = _missing_cause(conn, entry, rows)
    if cause:
        rows.append({"actor": "система", "action": "to_review", "label": ACTIONS["to_review"],
                     "created_at": entry["updated_at"], "detail": cause})
    return {"e": entry, "items": items, "history": rows, "docs": docs}


def _review_entry(conn: Connection, faq_id: int) -> dict:
    row = F.get_faq(conn, faq_id)
    if row is None:
        raise VersionError("Запись FAQ не найдена.")
    if row["status"] != "review":
        raise VersionError("Запись не на проверке.")
    _backfill(conn, row)
    return row


def confirm_entry(conn: Connection, faq_id: int, actor: str) -> None:
    with conn.transaction():
        _review_entry(conn, faq_id)
        _call(F.set_faq_status, conn, faq_id, "active")
        log(conn, faq_id, actor, "confirm", {"basis": F.get_faq(conn, faq_id)["basis"]})


def edit_answer(conn: Connection, faq_id: int, actor: str, answer: str) -> None:
    answer = (answer or "").strip()
    if not answer:
        raise VersionError("Ответ не может быть пустым.")
    with conn.transaction():
        row = _review_entry(conn, faq_id)
        conn.execute("UPDATE faq_entries SET answer=%s WHERE id=%s", (answer, faq_id))
        _call(F.set_faq_status, conn, faq_id, "active")
        log(conn, faq_id, actor, "edit", {"old_answer": row["answer"], "new_answer": answer})


def repin_entry(conn: Connection, faq_id: int, actor: str, doc_id: str, number: str) -> None:
    doc_id, number = (doc_id or "").strip(), (number or "").strip()
    if not doc_id or not number:
        raise VersionError("Укажите документ и номер пункта.")
    with conn.transaction():
        row = _review_entry(conn, faq_id)
        basis, first, version_id = _call(F._capture_basis, conn, "points", [(doc_id, number)])
        conn.execute("UPDATE faq_entries SET basis_kind='points', basis=%s, doc_id=%s, version_id=%s, "
                     "status='active', updated_at=now(), reviewed_at=now() WHERE id=%s",
                     (Jsonb(basis), first, version_id, faq_id))
        log(conn, faq_id, actor, "repin", {"old_basis": row["basis"], "new_basis": basis})


def delete_entry(conn: Connection, faq_id: int, actor: str) -> None:
    with conn.transaction():
        row = F.get_faq(conn, faq_id)
        if row is None or row["status"] == "deleted":
            raise VersionError("Запись FAQ не найдена.")
        _backfill(conn, row)
        F.set_faq_status(conn, faq_id, "deleted")
        log(conn, faq_id, actor, "delete", {"question": row["question"]})


def _call(fn, *args):
    try:
        return fn(*args)
    except F.TicketError as exc:
        raise VersionError(str(exc)) from exc
