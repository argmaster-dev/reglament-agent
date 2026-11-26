"""The conversation entry point shared by chat channels."""

import asyncio
import logging
import os
import uuid
from contextvars import ContextVar
from copy import deepcopy
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
from reglament_agent.model.queue import generation_progress
from reglament_agent.model.ollama import ModelError, ModelOutputError, OllamaClient
from reglament_agent.quality.faq import PublishedFaqStore
from reglament_agent.quality.mail import MailSettings
from reglament_agent.quality.tickets import create_ticket
from reglament_agent.storage.retrieve import retrieve
from reglament_agent.storage.search import preview

from . import context, grounding, relevance, state
from .lexicon import fold, words

_LOG = logging.getLogger(__name__)
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
        self.profile_max_age_days = int(os.getenv("DIALOG_PROFILE_MAX_AGE_DAYS", "30"))

    async def answer(self, user, message, on_status):
        return await self._turn(user, message.strip(), on_status, choice=False)

    async def choose(self, user, option, on_status):
        return await self._turn(user, option.strip(), on_status, choice=True)

    async def _turn(self, user, message, status, choice):
        started = monotonic()
        trace = {"steps": [], "calls": [], "candidates": [], "dropped": []}
        token = _records.set(trace["calls"])
        try:
            conn = await asyncio.to_thread(psycopg.connect, self.dsn)
            try:
                # Lock only this user's thread. Other conversations can proceed independently.
                initial = await asyncio.to_thread(state.lock_topic, conn, user)
                prior = state.previous(conn, user)
                if prior and prior.outcome != Outcome.ERROR:
                    return prior
                if prior:
                    conn.execute("DELETE FROM dialog_requests WHERE id=%s", (prior.answer_id,))
                request_id = str(uuid.uuid4())
                topic = deepcopy(initial)
                try:
                    # A failed model call must not confirm the profile or consume an attempt.
                    with conn.transaction():
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
                await asyncio.to_thread(conn.close)
        except psycopg.Error as exc:
            _LOG.warning("Dialog database operation failed (%s)", type(exc).__name__)
            return Answer(Outcome.ERROR)
        finally:
            _records.reset(token)

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
        confirming = topic.get("confirm_department")
        dept = context.department_in(message, departments or (options if axis == "department" else []))
        declared_dept = context.personal_department(message, dept)
        position = context.personal_position(message)
        explicit_stage = context.stage_in(message)
        pending = bool(options)
        unknown = context.unknown_reply(message)
        short = context.scope_only(message, dept)
        stage_reply = explicit_stage and len(words(message)) <= 4
        confirming_yes = pending and confirming and context.normalized_reply(message) == "да"
        follow = bool(topic.get("subject")) and bool(
            short
            or stage_reply
            or (pending and (choice or unknown or confirming_yes or not context.new_question(message)))
        )
        profile = state.get_profile(conn, user.email)
        if position and not dept:
            state.update_profile(conn, user.email, position=position)
            note = f"Запомнил должность: {position}."
            trace["steps"].append("remember_position")
            if pending:
                prompt = Clarification(topic["clarify"], options, len(topic.get("history", [])) + 1)
                reply = Answer(Outcome.CLARIFY, clarification=prompt, profile_note=note)
                return reply, topic["subject"], None
            return Answer(Outcome.ANSWER, note), message, None
        if declared_dept and short and not pending:
            state.set_department(conn, user.email, declared_dept)
            topic.pop("ignored_department", None)
            note = f"Запомнил подразделение: {declared_dept}."
            trace["steps"].append("remember_department")
            return Answer(Outcome.ANSWER, note), message, None

        question = topic["subject"] if follow else message
        if not follow:
            ignored = topic.get("ignored_department")
            offered = topic.get("profile_confirmation_offered")
            topic.clear()
            if ignored:
                topic["ignored_department"] = ignored
            if offered:
                topic["profile_confirmation_offered"] = True
        if pending and follow:
            topic.setdefault("history", []).append({"question": topic["clarify"], "answer": message})
        trace["restated"] = question
        variants = pending and follow and unknown
        note = None
        if follow and confirming_yes:
            profile = state.confirm_department(conn, user.email, confirming)
            dept = profile.department
            if dept == confirming:
                note = f"Подтвердил подразделение: {dept}."
            if not dept:
                # Another conversation may have reset the profile while this prompt was open.
                topic.pop("department", None)
                variants = True
        elif (
            follow
            and pending
            and axis == "department"
            and dept in options
            and context.named_department(message, dept)
        ):
            declared_dept = dept
        if declared_dept:
            profile = state.update_profile(conn, user.email, department=declared_dept)
            note = f"Запомнил подразделение: {declared_dept}."
            topic.pop("ignored_department", None)
        if follow and confirming and unknown and profile.department == confirming:
            topic["ignored_department"] = confirming

        origin = ScopeOrigin.NONE
        if dept:
            temporary = short and not context.named_department(message, dept) and not declared_dept
            origin = (
                ScopeOrigin.CLARIFICATION if pending and follow and not temporary else ScopeOrigin.QUESTION
            )
        elif follow and (not variants or axis == "stage") and topic.get("department"):
            dept = topic["department"]
            origin = ScopeOrigin.CLARIFICATION
        elif profile.department and not variants and profile.department != topic.get("ignored_department"):
            dept, origin = profile.department, ScopeOrigin.PROFILE
        stage = explicit_stage or (topic.get("stage") if follow else None) or context.stage_in(question)
        scope = Scope(dept, stage, origin)
        topic.update(subject=question, department=dept, stage=stage)

        def clarify(available, dimension="department", *, ask=None, confirm=None):
            attempt = len(topic.get("history", [])) + 1
            if attempt > 3:
                reply = Answer(Outcome.NO_BASIS, "Не удалось определить область вопроса.", scope=scope)
                reply.ticket = self._ticket(conn, user, message, question, "clarify_exhausted", topic, trace)
                topic.pop("options", None)
                topic.pop("confirm_department", None)
                return reply
            if ask is None:
                ask = (
                    "На каком этапе возник вопрос?"
                    if dimension == "stage"
                    else "Ответ зависит от подразделения. В каком вы работаете?"
                )
            topic.update(options=available, clarify=ask, axis=dimension, confirm_department=confirm)
            trace["steps"].append("clarify_" + dimension)
            return Answer(
                Outcome.CLARIFY,
                clarification=Clarification(ask, available, attempt),
                scope=scope,
                profile_note=note,
            )

        if pending and follow and not variants:
            resolved = (
                explicit_stage
                if axis == "stage"
                else declared_dept or confirming_yes or (dept if origin == ScopeOrigin.QUESTION else None)
            )
            if not resolved:
                return clarify(options, axis, ask=topic["clarify"], confirm=confirming), question, None
        topic.pop("options", None)
        topic.pop("confirm_department", None)
        definition = context.definition(question)
        if definition:
            return Answer(Outcome.ANSWER, definition, scope=scope), question, None

        if (
            origin == ScopeOrigin.PROFILE
            and not follow
            and not topic.get("profile_confirmation_offered")
            and context.profile_expired(profile, self.profile_max_age_days)
        ):
            topic["profile_confirmation_offered"] = True
            reply = clarify(
                ["Да", *departments], ask=f"Ваше подразделение по-прежнему «{dept}»?", confirm=dept,
            )
            return reply, question, None

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
            rows = grounding.anchors(points)
            related = relevance.applicable_refs(question, rows)
            by_department, by_stage = {}, {}
            for anchor in rows:
                if anchor.ref not in related:
                    continue
                sentence = tuple(words(anchor.text))
                by_department.setdefault(anchor.ref.department, set()).add(sentence)
                for code in stages_by_doc.get(anchor.ref.doc_id, []):
                    by_stage.setdefault(code, set()).add(sentence)
            if not dept and len({frozenset(rows) for rows in by_department.values()}) > 1:
                return clarify(departments or sorted(by_department))
            if not stage and len({frozenset(rows) for rows in by_stage.values()}) > 1:
                available = [label for code, label in context.STAGE_LABELS.items() if code in by_stage]
                if len(available) > 1:
                    return clarify(available, "stage")
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
        async with generation_progress(status) as notify:
            response = await self.model.chat_json(
                messages,
                schema,
                step="select",
                request_id=request_id,
                on_position=notify,
                on_start=lambda: notify(0),
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
