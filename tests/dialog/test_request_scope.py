"""Distinguish staff requests for live work from questions about company rules."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_route import Model, hit, status


class FirstSource(Model):
    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        sources = json.loads(messages[1]["content"])["источники"]
        claims = [{"ref": s["ref"], "text": s["цитата"]} for s in sources[:1]]
        return SimpleNamespace(data={"outcome": "answer" if claims else "no_basis", "claims": claims})


@pytest.mark.parametrize("question", [
    "Какая у меня будет маржа по сделке?",
    "Покажи мою фактическую премию за квартал.",
    "Сколько прибыли принесла эта сделка?",
    "Сколько сегодня стоит доллар?",
    "Какой курс евро?",
    "Отправь клиенту напоминание о встрече.",
    "Сделай follow-up по вчерашнему созвону.",
])
def test_request_for_live_values_or_work_opens_no_ticket(isolated, question):
    conn, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates([], []), shortlist=lambda *a, **kw: [])
    user = UserContext("reader@example.org", "employee", "dialog", "ask")
    answer = asyncio.run(route.answer(user, question, status))
    assert answer.outcome == Outcome.OFF_TOPIC and "руководител" in answer.summary
    assert not answer.ticket and not model.calls
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (0,)


@pytest.mark.parametrize("question", [
    "Где указан порядок согласования спецификации, когда маржинальность услуг ниже целевой?",
    "Как рассчитать мою премию по этой сделке?",
    "Кто отвечает за follow-up по сделке?",
    "Какой курс доллара применять при оформлении договора?",
])
def test_procedure_still_searches_the_rules(isolated, question):
    _, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates([], []), shortlist=lambda *a, **kw: [])
    answer = asyncio.run(route.answer(UserContext("u@example.org", "employee", "1", "1"), question, status))
    assert model.calls and answer.outcome == Outcome.NO_BASIS and answer.ticket


def test_partial_answer_keeps_the_verified_fact_and_creates_one_ticket(isolated):
    conn, dsn = isolated
    model = FirstSource()
    source = hit("Сервис", "Диспетчер создаёт карточку осмотра.")
    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates([source], []), shortlist=lambda *a, **kw: [])
    user = UserContext("u@example.org", "employee", "1", "1")
    q = "Кто создаёт карточку осмотра и какой штраф начисляется за просрочку?"
    answer = asyncio.run(route.answer(user, q, status))
    assert answer.outcome == Outcome.LOW_CONFIDENCE
    assert [s.text for s in answer.statements] == [source.text]
    assert answer.statements[0].sources[0].point == "4.1"
    assert "штраф" in answer.caveat and answer.ticket
    assert asyncio.run(route.answer(user, q, status)) == answer
    assert conn.execute("SELECT reason FROM tickets").fetchall() == [("low_confidence",)]
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (1,)
