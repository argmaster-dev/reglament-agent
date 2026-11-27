"""Persistent, user-scoped conversation state and request journal."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from reglament_agent.answer import Answer, Outcome, Profile
from .lexicon import query_cache_input

answers = TypeAdapter(Answer)


def get_profile(conn, email):
    row = conn.execute(
        "SELECT department, position, confirmed_at FROM dialog_profiles WHERE email=%s", (email,)
    ).fetchone()
    if row is None:
        return Profile(email, None, None, None)
    department, position, confirmed_at = row
    return Profile(email, department, position, confirmed_at.isoformat() if confirmed_at else None)


def update_profile(conn, email, *, department=None, position=None):
    conn.execute(
        """INSERT INTO dialog_profiles (email, department, position, confirmed_at)
           VALUES (%s,%s,%s,now())
           ON CONFLICT(email) DO UPDATE SET
                department=coalesce(excluded.department, dialog_profiles.department),
                position=coalesce(excluded.position, dialog_profiles.position),
                confirmed_at=CASE WHEN excluded.department IS NOT NULL
                    OR dialog_profiles.department IS NULL THEN excluded.confirmed_at
                    ELSE dialog_profiles.confirmed_at END""",
        (email, department, position),
    )
    return get_profile(conn, email)


def set_department(conn, email, department):
    return update_profile(conn, email, department=department)


def reset_profile(conn, email):
    conn.execute("DELETE FROM dialog_profiles WHERE email=%s", (email,))


def confirm_department(conn, email, department):
    conn.execute(
        "UPDATE dialog_profiles SET confirmed_at=now() WHERE email=%s AND department=%s",
        (email, department),
    )
    return get_profile(conn, email)


class Profiles:
    def __init__(self, dsn):
        self.dsn = dsn

    async def run(self, fn, *args):
        def call():
            with psycopg.connect(self.dsn) as conn:
                return fn(conn, *args)

        return await asyncio.to_thread(call)

    async def get(self, email):
        return await self.run(get_profile, email)

    async def reset(self, email):
        await self.run(reset_profile, email)

    async def set_department(self, email, department):
        return await self.run(set_department, email, department)


def lock_topic(conn, user):
    key = (user.email, user.dialog_id)
    conn.execute("INSERT INTO dialog_topics(user_email,dialog_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", key)
    return conn.execute(
        "SELECT state FROM dialog_topics WHERE user_email=%s AND dialog_id=%s FOR UPDATE", key
    ).fetchone()[0]


def previous(conn, user):
    row = conn.execute(
        """SELECT answer FROM dialog_requests WHERE user_email=%s AND dialog_id=%s
                          AND message_id=%s""",
        (user.email, user.dialog_id, user.message_id),
    ).fetchone()
    return answers.validate_python(row[0]) if row else None


POLICY = "extract-v7-document-subject"


def _versions(conn):
    versions = conn.execute(
        "SELECT doc_id,active_version_id FROM documents WHERE status='active' ORDER BY doc_id"
    ).fetchall()
    faq = conn.execute(
        "SELECT id,updated_at::text FROM faq_entries WHERE status='active' ORDER BY id"
    ).fetchall()
    return versions, faq


def cache_key(conn, user, question, scope, model):
    """Shared exact key: policy, model, role, scope, question and the active documents and FAQ.

    The user's address and name are deliberately not part of it. A new document version or a changed
    FAQ entry changes the key, so the old answer is never found again.
    """
    versions, faq = _versions(conn)
    data = [POLICY, model, user.role, asdict(scope), query_cache_input(question), versions, faq]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def cache_context(conn, user, scope, model):
    """The key without the question: entries of one context are candidates for a similar-phrasing hit."""
    versions, faq = _versions(conn)
    data = [POLICY, model, user.role, asdict(scope), versions, faq]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


@dataclass
class CacheSlot:
    """Where a cacheable answer is filed: exact key, context and the embedding of its question."""

    key: str
    context: str
    vector: list | None = None


SHAREABLE = (Outcome.ANSWER, Outcome.EXPERT_ANSWER)


def shareable(stored):
    """Only the content of an answer crosses between users.

    Ticket, profile note, answer id, clarification and everything else that belongs to the earlier
    request or its author are dropped here, by construction: the copy is built from an allow-list.
    """
    if stored.outcome not in SHAREABLE or stored.caveat:
        return None
    return Answer(
        stored.outcome, stored.summary, stored.statements, stored.variants, scope=stored.scope,
        related=stored.related, from_cache=True,
    )


def cached(conn, key):
    """The newest shareable answer saved under this key by anyone, as (answer, request id)."""
    row = conn.execute(
        "SELECT answer, id FROM dialog_requests WHERE cache_key=%s ORDER BY created_at DESC LIMIT 1", (key,)
    ).fetchone()
    copy = row and shareable(answers.validate_python(row[0]))
    return (copy, str(row[1])) if copy else None


def save(conn, user, state, message, question, result, trace, key):
    slot = key if isinstance(key, CacheSlot) else None
    key = slot.key if slot else key
    conn.execute(
        "UPDATE dialog_topics SET state=%s WHERE user_email=%s AND dialog_id=%s",
        (Jsonb(state), user.email, user.dialog_id),
    )
    conn.execute(
        """INSERT INTO dialog_requests(id,user_email,dialog_id,message_id,question,restated,
                    answer,trace,cache_key,cache_context,cache_embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            result.answer_id,
            user.email,
            user.dialog_id,
            user.message_id,
            message,
            question,
            Jsonb(answers.dump_python(result, mode="json")),
            Jsonb(trace),
            key,
            slot.context if slot else None,
            slot.vector if slot else None,
        ),
    )
