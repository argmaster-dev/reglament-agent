"""The conversation entry point shared by chat channels."""

import asyncio
import uuid
from contextvars import ContextVar
from dataclasses import asdict, replace
from time import monotonic

import psycopg

from reglament_agent.answer import (
    Answer,
    Clarification,
    Outcome,
    Scope,
    ScopeOrigin,
    Statement,
    Variant,
)
from reglament_agent.model.ollama import ModelError, ModelOutputError, OllamaClient
from reglament_agent.quality.faq import PublishedFaqStore
from reglament_agent.quality.mail import MailSettings
from reglament_agent.quality.tickets import create_ticket
from reglament_agent.storage.retrieve import retrieve
from reglament_agent.storage.search import preview

from . import context, grounding, state
from .lexicon import fold, words

_records = ContextVar("dialog_model_calls", default=None)
_ESCALATE = {Outcome.NO_BASIS, Outcome.LOW_CONFIDENCE, Outcome.CONFLICT, Outcome.EXTERNAL_DOC}


def describe(points):
    return [
        {
            "doc_id": p.doc_id,
            "title": p.doc_title,
            "point": p.number,
            "department": p.department,
            "version": p.version_no,
            "score": float(p.score),
        }
        for p in points
    ]


class RequestCalls:
    def record(self, call):
        records = _records.get()
        if records is not None:
            records.append(asdict(call))


class Dialog:
    def __init__(self, dsn, model=None, *, search=retrieve, shortlist=preview, mail=None):
        self.dsn = dsn
        self.model = model or OllamaClient(sink=RequestCalls())
        self.shortlist = shortlist
        self.search = search
        self.mail = mail or MailSettings()
        self.profiles = state.Profiles(dsn)

    async def answer(self, user, message, on_status):
        return await self._turn(user, message.strip(), on_status, choice=False)

    async def choose(self, user, option, on_status):
        return await self._turn(user, option.strip(), on_status, choice=True)

    async def _turn(self, user, message, status, choice):
        # Lock only this user's thread. Another employee can search while the model slot is busy.
        conn = await asyncio.to_thread(psycopg.connect, self.dsn)
        started = monotonic()
        trace = {"steps": [], "calls": [], "candidates": [], "dropped": []}
        token = _records.set(trace["calls"])
        try:
            topic = await asyncio.to_thread(state.lock_topic, conn, user)
            prior = state.previous(conn, user)
            if prior and prior.outcome != Outcome.ERROR:
                return prior
            if prior:
                conn.execute("DELETE FROM dialog_requests WHERE id=%s", (prior.answer_id,))
            request_id = str(uuid.uuid4())
            initial = topic.copy()
            try:
                result, question, key = await self._resolve(
                    conn, user, message, choice, status, topic, trace, request_id
                )
            except ModelError as exc:
                trace["steps"].append(type(exc).__name__)
                topic, result, question, key = initial, Answer(Outcome.ERROR), message, None
            trace["elapsed_ms"] = round((monotonic() - started) * 1000)
            result.answer_id = request_id
            state.save(conn, user, topic, message, question, result, trace, key)
            await asyncio.to_thread(conn.commit)
            return result
        finally:
            _records.reset(token)
            await asyncio.to_thread(conn.close)

    def _ticket(self, conn, user, message, question, reason, topic, trace):
        profile = state.get_profile(conn, user.email)
        return create_ticket(
            conn,
            user=user,
            question=message,
            restated=question,
            reason=reason,
            profile={"department": profile.department, "position": profile.position},
            clarifications=topic.get("history", []),
            candidates=trace["candidates"][:12],
            service_email=self.mail.service_email,
            public_base_url=self.mail.public_base_url,
        )

    async def _resolve(self, conn, user, message, choice, status, topic, trace, request_id):
        trace["steps"].append("resolve_scope")
        cards = conn.execute(
            "SELECT doc_id,department,stages FROM documents WHERE status='active'"
        ).fetchall()
        departments = sorted({dept for _, dept, _ in cards})
        stages_by_doc = {doc: stages for doc, _, stages in cards}
        options = topic.get("options", [])
        axis = topic.get("axis", "department")
        dept = context.department_in(message, departments or (options if axis == "department" else []))
        explicit_stage = context.stage_in(message)
        pending = bool(options)
        unknown = fold(message).strip(".!? ") in {"не знаю", "не уверен", "не уверена"}
        short = context.scope_only(message, dept)
        stage_reply = explicit_stage and len(words(message)) <= 4
        follow = bool(topic.get("subject") and (short or stage_reply or (pending and (choice or unknown))))
        profile = state.get_profile(conn, user.email)
        old_question = topic.get("subject", "")
        if pending and not (follow or "?" in message or len(words(message)) >= 6):
            follow = True
        question = old_question if follow else message
        if not follow:
            topic.clear()
        if pending and follow:
            topic.setdefault("history", []).append({"question": topic["clarify"], "answer": message})
        trace["restated"] = question
        variants = follow and unknown
        origin = ScopeOrigin.NONE
        if dept:
            origin = ScopeOrigin.CLARIFICATION if pending and follow else ScopeOrigin.QUESTION
        elif follow and axis == "stage" and topic.get("department"):
            dept, origin = topic["department"], ScopeOrigin.CLARIFICATION
        elif profile.department and not variants:
            dept, origin = profile.department, ScopeOrigin.PROFILE
        stage = explicit_stage or context.stage_in(question)
        scope = Scope(dept, stage, origin)
        note = None
        if dept and (
            context.personal(message)
            or (
                axis == "department"
                and pending
                and follow
                and short
                and (choice or not message.lower().startswith(("а ", "а в ")))
            )
        ):
            state.set_department(conn, user.email, dept)
            note = f"Запомнил подразделение: {dept}."

        topic["subject"] = question

        def clarify(available, dimension="department"):
            attempt = len(topic.get("history", [])) + 1
            if attempt > 3:
                reply = Answer(Outcome.NO_BASIS, "Не удалось определить область вопроса.", scope=scope)
                reply.ticket = self._ticket(conn, user, message, question, "clarify_exhausted", topic, trace)
                topic.pop("options", None)
                return reply
            ask = "Ответ зависит от подразделения. В каком вы работаете?"
            if dimension == "stage":
                ask = "На каком этапе возник вопрос?"
            topic.update(options=available, clarify=ask, axis=dimension, department=dept)
            trace["steps"].append("clarify_" + dimension)
            return Answer(Outcome.CLARIFY, clarification=Clarification(ask, available, attempt), scope=scope)

        if pending and follow and not variants and not (stage if axis == "stage" else dept):
            return clarify(options, axis), question, None
        topic.pop("options", None)
        if note and context.personal(message) and not follow and short:
            return Answer(Outcome.ANSWER, note, scope=scope), question, None
        definition = context.definition(question)
        if definition:
            return Answer(Outcome.ANSWER, definition, scope=scope), question, None

        key = None
        if not follow:
            model_name = getattr(
                getattr(self.model, "settings", None), "chat_model", type(self.model).__name__
            )
            key = state.cache_key(conn, user, question, scope, model_name)
            hit = state.cached(conn, user, key)
            if hit:
                trace["steps"].append("cache_hit")
                return replace(hit, from_cache=True, profile_note=note), question, None

        await status("Ищу основания в действующих регламентах…")

        def ambiguity(points):
            refs = grounding.applicable_refs(question, grounding.anchors(points))
            possible = {r.department for r in refs}
            if not dept and len(possible) > 1:
                return clarify(departments or sorted(possible))
            if not stage and len(possible) == 1:
                exclusive = {stages[0] for r in refs if len(stages := stages_by_doc.get(r.doc_id, [])) == 1}
                if len(exclusive) > 1:
                    return clarify([context.STAGE_LABELS[k] for k in sorted(exclusive)], "stage")
            return None

        if not variants and (not dept or not stage):
            candidates = await asyncio.to_thread(self.shortlist, conn, question, department=dept, stage=stage)
            clarification = ambiguity(candidates)
            if clarification:
                trace["candidates"] = describe(candidates)
                return clarification, question, None
        trace["steps"].append("retrieve")
        (vector,) = await self.model.embed([question], step="retrieve", request_id=request_id)
        found = await asyncio.to_thread(
            self.search,
            conn,
            question,
            vector,
            department=dept,
            stage=stage,
            limit=28,
            faq=PublishedFaqStore(),
        )
        trace["candidates"] = describe(found.points)
        normalized = words(question)
        for faq in found.faq:
            if any(words(q) == normalized for q in (faq.question, *faq.alt_questions)):
                refs = [faq.source] + ([faq.basis] if faq.basis else [])
                reply = Answer(
                    Outcome.EXPERT_ANSWER,
                    statements=[Statement(faq.answer, refs)],
                    scope=scope,
                    profile_note=note,
                )
                trace["steps"].append("published_faq")
                return reply, question, key
        rows = grounding.anchors(found.points)
        if not departments:
            departments = sorted({p.department for p in found.points})
        if not variants:
            clarification = ambiguity(found.points)
            if clarification:
                return clarification, question, None
        items = grounding.vocabulary(question, rows)
        messages, schema = grounding.prompt(question, items, variants)
        await status("Проверяю ответ по найденным пунктам…")
        trace["steps"].append("select")
        response = await self.model.chat_json(
            messages,
            schema,
            step="select",
            request_id=request_id,
            options={"num_predict": 1400, "temperature": 0},
        )
        data = response.data
        if (
            data.get("outcome") not in grounding.MODEL_OUTCOMES
            or not isinstance(data.get("claims"), list)
            or any(
                not isinstance(c, dict)
                or not isinstance(c.get("ref"), (dict, type(None)))
                or any(
                    not isinstance(c.get(k, ""), str)
                    for k in ("actor", "action", "object", "qualifier", "text")
                )
                or type(c.get("negated", False)) is not bool
                for c in data["claims"]
            )
        ):
            raise ModelOutputError("invalid claim structure")
        trace["proposal"] = data
        trace["evidence"] = {key: asdict(a.ref) for key, a in items.items()}
        result = grounding.assemble(question, response.data, items, departments, variants, trace, rows)
        if variants and axis == "stage" and result.outcome == Outcome.VARIANTS:
            statements = [s for v in result.variants for s in v.statements]
            result.variants = [
                Variant(label, [s for s in statements if code in stages_by_doc.get(s.sources[0].doc_id, [])])
                for code, label in context.STAGE_LABELS.items()
            ]
        trace["steps"].append("verify")
        result.scope, result.profile_note = scope, note
        if result.outcome in _ESCALATE:
            result.ticket = self._ticket(conn, user, message, question, result.outcome.value, topic, trace)
            trace["steps"].append("ticket")
        cacheable = result.outcome == Outcome.ANSWER and not result.caveat
        return result, question, key if cacheable else None

    async def rate(self, user, answer_id, good, reason, forward=False):
        def record(conn):
            try:
                identity = uuid.UUID(answer_id)
            except (TypeError, ValueError):
                return None
            row = conn.execute(
                """UPDATE dialog_requests SET rating=%s,feedback=%s WHERE id=%s
                                  AND user_email=%s AND dialog_id=%s RETURNING question,restated,trace""",
                (good, reason, identity, user.email, user.dialog_id),
            ).fetchone()
            if row and forward and not good:
                dispute = replace(user, message_id=f"feedback:{identity}")
                return self._ticket(
                    conn,
                    dispute,
                    row[0],
                    row[1],
                    "user_dispute",
                    {"history": [{"question": "Причина несогласия", "answer": reason or ""}]},
                    row[2],
                )

        return await self.profiles.run(record)
