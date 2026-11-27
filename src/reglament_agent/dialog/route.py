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
    SourceRef,
    Statement,
    Variant,
)
from reglament_agent.model.queue import generation_progress
from reglament_agent.model.ollama import ModelError, ModelOutputError, OllamaClient
from reglament_agent.quality.faq import PublishedFaqStore, match_published
from reglament_agent.quality.mail import MailSettings
from reglament_agent.quality.tickets import create_ticket
from reglament_agent.storage.retrieve import Candidates, retrieve
from reglament_agent.storage.search import preview
from reglament_agent.storage.sources import active_process

from . import context, grounding, guards, processes, relevance, spelling, state
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
        self._vocabulary = (None, None)

    def vocabulary(self, conn):
        """Words of the active regulations; rebuilt only when the set of active versions changes."""
        versions = tuple(
            conn.execute("SELECT active_version_id FROM documents WHERE status='active' ORDER BY doc_id").fetchall()
        )
        if self._vocabulary[0] != versions:
            texts = [
                r[0]
                for r in conn.execute(
                    "SELECT p.text FROM points p JOIN documents d ON d.active_version_id=p.version_id"
                    " WHERE d.status='active'"
                )
            ]
            departments = [r[0] for r in conn.execute("SELECT DISTINCT department FROM documents")]
            self._vocabulary = (versions, spelling.Vocabulary.from_texts(texts + departments))
        return self._vocabulary[1]

    async def answer(self, user, message, on_status):
        return await self._turn(user, message.strip(), on_status, choice=False)

    async def choose(self, user, option, on_status):
        return await self._turn(user, option.strip(), on_status, choice=True)

    async def _turn(self, user, message, status, choice):
        refusal = Answer(
            Outcome.OFF_TOPIC, f"Вопрос должен содержать от {guards.MIN_LENGTH} до {guards.MAX_LENGTH} символов."
        )
        if len(message) > guards.MAX_LENGTH or not message:
            return refusal
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
                if len(message) < guards.MIN_LENGTH and not initial.get("options"):
                    # A short reply is valid only as an answer to an open clarification.
                    return refusal
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

    @staticmethod
    def _merge_variants(named, faqs, assembled):
        by_department = {v.department: v for v in assembled}
        merged = []
        for d in named:
            faq = faqs.get(d)
            if faq:
                refs = [faq.source] + ([faq.basis] if faq.basis else [])
                merged.append(Variant(d, [Statement(faq.answer, refs)], faq.attribution or None))
            elif d in by_department and by_department[d].statements:
                merged.append(by_department[d])
            else:
                merged.append(Variant(d, [], f"в регламентах {d} нет основания"))
        return merged

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
        option_refs = [SourceRef(**r) if r else None for r in topic.get("option_refs", [])]
        selected = None
        if axis == "intent":
            selected = next((ref for label, ref in zip(options, option_refs)
                             if context.normalized_reply(label) == context.normalized_reply(message)), None)
        confirming = topic.get("confirm_department")
        catalog = departments or (options if axis == "department" else [])
        dept = context.department_in(message, catalog)
        named = [] if topic.get("options") else context.departments_in(message, departments)
        # A question about several departments is answered per department, never from one of them.
        multi = len(named) >= 2
        if multi:
            dept = None
        declared_dept = context.personal_department(message, dept)
        position = context.personal_position(message)
        explicit_stage = context.stage_in(message)
        pending = bool(options)
        unknown = context.unknown_reply(message)
        short = context.scope_only(message, dept)
        stage_reply = explicit_stage and len(words(message)) <= 4
        confirming_yes = pending and confirming and context.normalized_reply(message) == "да"
        redirect = guards.redirect_for(message)
        follow = redirect is None and bool(topic.get("subject")) and bool(
            short
            or stage_reply
            or (pending and (selected or choice or unknown or confirming_yes or not context.new_question(message)))
        )
        profile = state.get_profile(conn, user.email)
        note = None
        if position and not dept and redirect is None:
            state.update_profile(conn, user.email, position=position)
            note = f"Запомнил должность: {position}."
            trace["steps"].append("remember_position")
            if not pending:
                return Answer(Outcome.ANSWER, note), message, None
            follow = True
        if declared_dept and short and not pending and redirect is None:
            state.set_department(conn, user.email, declared_dept)
            topic.pop("ignored_department", None)
            note = f"Запомнил подразделение: {declared_dept}."
            trace["steps"].append("remember_department")
            return Answer(Outcome.ANSWER, note), message, None

        question = topic["subject"] if follow else message
        invention = bool(topic.get("invention")) if follow else guards.fabrication_intent(message)
        if not follow:
            question = guards.neutralize(question)
            if invention:
                question = guards.strip_invention(question)
            if redirect is None:
                question = spelling.correct(question, self.vocabulary(conn))
        if not follow:
            ignored = topic.get("ignored_department")
            offered = topic.get("profile_confirmation_offered")
            topic.clear()
            if ignored:
                topic["ignored_department"] = ignored
            if offered:
                topic["profile_confirmation_offered"] = True
            topic["invention"] = invention
        if pending and follow:
            topic.setdefault("history", []).append({"question": topic["clarify"], "answer": message})
        trace["restated"] = question
        if redirect:
            trace["steps"].append("off_topic")
            summary = guards.LEGAL_MESSAGE if redirect == "legal" else guards.OFF_TOPIC_MESSAGE
            return Answer(Outcome.OFF_TOPIC, summary, redirect=redirect), question, None
        variants = (pending and follow and unknown and axis != "intent") or multi
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
        if selected:
            dept, origin = selected.department, ScopeOrigin.CLARIFICATION
        elif dept:
            temporary = short and not context.named_department(message, dept) and not declared_dept
            origin = (
                ScopeOrigin.CLARIFICATION if pending and follow and not temporary else ScopeOrigin.QUESTION
            )
        elif follow and (not variants or axis == "stage") and topic.get("department"):
            dept = topic["department"]
            origin = ScopeOrigin.CLARIFICATION
        elif profile.department and not variants and profile.department != topic.get("ignored_department"):
            dept, origin = profile.department, ScopeOrigin.PROFILE
        stage = (None if selected else explicit_stage) or (topic.get("stage") if follow else None) or context.stage_in(question)
        scope = Scope(dept, stage, origin)
        topic.update(subject=question, department=dept, stage=stage)

        def clarify(available, dimension="department", *, ask=None, confirm=None, refs=()):
            attempt = len(topic.get("history", [])) + 1
            if attempt > 3:
                reply = Answer(Outcome.NO_BASIS, "Не удалось определить область вопроса.", scope=scope)
                reply.ticket = self._ticket(conn, user, message, question, "clarify_exhausted", topic, trace)
                topic.pop("options", None)
                topic.pop("option_refs", None)
                topic.pop("confirm_department", None)
                return reply
            if ask is None:
                ask = (
                    "На каком этапе возник вопрос?"
                    if dimension == "stage"
                    else "Ответ зависит от подразделения. В каком вы работаете?"
                )
            topic.update(options=available, clarify=ask, axis=dimension, confirm_department=confirm,
                         option_refs=[asdict(ref) if ref else None for ref in refs])
            trace["steps"].append("clarify_" + dimension)
            return Answer(
                Outcome.CLARIFY,
                clarification=Clarification(ask, available, attempt, list(refs), dimension),
                scope=scope,
                profile_note=note,
            )

        if pending and follow and not variants:
            if axis == "intent" and not selected:
                return clarify(options, axis, ask=topic["clarify"], refs=option_refs), question, None
            resolved = selected or (
                explicit_stage
                if axis == "stage"
                else declared_dept or confirming_yes or (dept if origin == ScopeOrigin.QUESTION else None)
            )
            if not resolved:
                return clarify(options, axis, ask=topic["clarify"], confirm=confirming), question, None
        topic.pop("options", None)
        topic.pop("option_refs", None)
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
            options = processes.options(question, points)
            if options:
                return clarify([name for name, _ in options], "intent", ask="Какой процесс вас интересует?",
                               refs=[ref for _, ref in options])
            rows = grounding.anchors(points)
            related = relevance.applicable_refs(question, rows, department=dept)
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

        if not selected and not variants and (not dept or not stage):
            candidates = await asyncio.to_thread(self.shortlist, conn, question, department=dept, stage=stage)
            clarification = ambiguity(candidates)
            if clarification:
                trace["candidates"] = describe(candidates)
                return clarification, question, None
        trace["steps"].append("retrieve")
        if selected:
            points = await asyncio.to_thread(active_process, conn, selected)
            if not points:
                reply = Answer(Outcome.NO_BASIS, "Выбранный пункт больше не входит в действующую редакцию.", scope=scope)
                reply.ticket = self._ticket(conn, user, message, question, "no_basis", topic, trace)
                return reply, question, None
            question = f"{question.rstrip(' ?')} — {points[0].section_title}?"
            trace["restated"] = question
            trace["steps"].append("selected_process")
            found = Candidates(points, [])
        else:
            (vector,) = await self.model.embed([question], step="retrieve", request_id=request_id)
        if multi:
            parts = [
                await asyncio.to_thread(
                    self.search, conn, question, vector, department=d, stage=stage,
                    limit=max(8, 28 // len(named)), faq=PublishedFaqStore(),
                )
                for d in named
            ]
            # A published answer settles only its own department; the others are assembled from the points.
            # Only an answer based on this department's own document may stand for it. An answer without such
            # a basis (outside the regulations) belongs to no department row and is not shown in the comparison.
            faqs = {
                d: match_published(
                    question, [f for f in part.faq if f.basis and f.basis.department == d]
                )
                for d, part in zip(named, parts)
            }
            departments = [d for d in named if not faqs[d]]
            found = Candidates(
                [p for d, part in zip(named, parts) if d in departments for p in part.points], []
            )
        elif not selected:
            found = await asyncio.to_thread(
                self.search, conn, question, vector, department=dept, stage=stage, limit=28,
                faq=PublishedFaqStore(),
            )
        trace["candidates"] = describe(found.points)
        faq = None if multi else match_published(question, found.faq)
        if faq is not None:
            refs = [faq.source] + ([faq.basis] if faq.basis else [])
            reply = Answer(
                Outcome.EXPERT_ANSWER,
                summary=faq.attribution,
                statements=[Statement(faq.answer, refs)],
                scope=scope,
                profile_note=note,
            )
            trace["steps"].append("published_faq")
            return reply, question, key
        if multi and not departments:
            reply = Answer(Outcome.VARIANTS, scope=scope, profile_note=note)
            reply.variants = self._merge_variants(named, faqs, [])
            trace["steps"].append("published_faq")
            return reply, question, None
        rows = grounding.anchors(found.points)
        if not departments:
            departments = sorted({p.department for p in found.points})
        if not variants and not selected:
            clarification = ambiguity(found.points)
            if clarification:
                return clarification, question, None
        items = grounding.vocabulary(question, rows)
        messages, schema = grounding.prompt(question, items, variants, invention)
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
        result = grounding.assemble(question, response.data, items, departments, variants, trace, rows, invention)
        if variants and not multi and axis == "stage" and result.outcome == Outcome.VARIANTS:
            statements = [s for v in result.variants for s in v.statements]
            result.variants = [
                Variant(label, [s for s in statements if code in stages_by_doc.get(s.sources[0].doc_id, [])])
                for code, label in context.STAGE_LABELS.items()
            ]
        trace["steps"].append("verify")
        if multi and result.outcome == Outcome.NO_BASIS and not any(faqs.values()):
            # Nothing for any named department: an ordinary refusal with a ticket, each department named.
            marks = [v.note for v in self._merge_variants(named, faqs, [])]
            result.summary = "\n".join([result.summary, *marks])
        elif multi and (result.outcome == Outcome.VARIANTS or any(faqs.values())):
            # A department without a basis is marked in its variant; it does not open a ticket.
            own = result.variants if result.outcome == Outcome.VARIANTS else []
            result = Answer(
                Outcome.VARIANTS, variants=self._merge_variants(named, faqs, own), caveat=result.caveat
            )
        if result.outcome == Outcome.NO_BASIS and not selected and relevance.request_qualifiers(question):
            # No silent substitution: name the points about the same noun for another object or direction,
            # including those of other departments.
            wider = rows
            if dept and not multi:
                found_all = await asyncio.to_thread(
                    self.search, conn, question, vector, department=None, stage=stage, limit=28,
                    faq=PublishedFaqStore(),
                )
                wider = grounding.anchors(found_all.points)
            near, result.nearby_sources = relevance.nearby_evidence(question, wider)
            if near:
                result.summary = (
                    f"{result.summary}\nТочного пункта для этого объекта и направления нет. "
                    f"Есть пункты о том же предмете, но для другого объекта или направления:\n{near}"
                )
        if result.outcome == Outcome.ANSWER:
            result.related = processes.related(result.statements, found.points)
        result.scope, result.profile_note = scope, note
        if result.outcome in _ESCALATE:
            result.ticket = self._ticket(conn, user, message, question, result.outcome.value, topic, trace)
            trace["steps"].append("ticket")
        cacheable = result.outcome == Outcome.ANSWER and not result.caveat
        return result, question, key if cacheable else None

    async def rate(self, user, answer_id, good, reason, forward=False):
        reason = reason[: guards.MAX_LENGTH] if reason else reason
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
