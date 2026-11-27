"""Persistent, user-scoped conversation state and request journal."""

import asyncio
import hashlib
import json
from dataclasses import asdict

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from reglament_agent.answer import Answer, Profile

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


def cache_key(conn, user, question, scope, model):
    versions = conn.execute(
        "SELECT doc_id,active_version_id FROM documents WHERE status='active' ORDER BY doc_id"
    ).fetchall()
    faq = conn.execute(
        "SELECT id,updated_at::text FROM faq_entries WHERE status='active' ORDER BY id"
    ).fetchall()
    data = ["extract-v3", model, user.role, asdict(scope), question.strip().casefold(), versions, faq]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def cached(conn, user, key):
    row = conn.execute(
        "SELECT answer FROM dialog_requests WHERE user_email=%s AND cache_key=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (user.email, key),
    ).fetchone()
    return answers.validate_python(row[0]) if row else None


def save(conn, user, state, message, question, result, trace, key):
    conn.execute(
        "UPDATE dialog_topics SET state=%s WHERE user_email=%s AND dialog_id=%s",
        (Jsonb(state), user.email, user.dialog_id),
    )
    conn.execute(
        """INSERT INTO dialog_requests(id,user_email,dialog_id,message_id,question,restated,
                    answer,trace,cache_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
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
        ),
    )
