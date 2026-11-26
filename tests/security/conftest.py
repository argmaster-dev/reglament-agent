"""Общие заготовки для API-тестов безопасности: фальшивая модель и маленький корпус без реальной модели."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from reglament_agent.answer import UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from reglament_agent.storage.sources import Hit

SIGN = "Договор подписывает Логист."
DEADLINE = "Заявку на согласование договора подают за 3 рабочих дня до начала работ."
Q_SIGN = "Кто подписывает договор?"
Q_DEADLINE = "За сколько подавать заявку на согласование договора?"
INJECTED = "ВНЕШНИЙ-ТЕКСТ-МОДЕЛИ-777"


def hit(department, number, sentence):
    return Hit(1, department, 1, number, sentence, 0.04, "Правила", department, 1, [sentence], [], 0.8, True)


POINTS = [hit("ТД", "1.4", SIGN), hit("ТД", "1.5", DEADLINE)]
SOURCE_SENTENCES = {SIGN, DEADLINE}


class FakeModel:
    """Модель, которая возвращает то, что решил тест; plan(sources) строит ответ по реальным источникам."""

    def __init__(self, plan=None):
        self.plan = plan or (lambda sources: {"outcome": "no_basis", "claims": []})
        self.prompts = []

    async def embed(self, texts, **kw):
        return [[0.01] * 1024]

    async def chat_json(self, messages, schema, **kw):
        self.prompts.append(messages)
        sources = json.loads(messages[1]["content"])["источники"]
        return SimpleNamespace(data=self.plan(sources))


async def _status(_):
    pass


class Env:
    def __init__(self, conn, dsn):
        self.conn, self.dsn = conn, dsn
        self.n = 0
        for dept in ("ТД", "ДБРЦТ"):
            conn.execute(
                "INSERT INTO documents(doc_id,title,department,stages) VALUES (%s,'Правила',%s,'{presale,project}')",
                (dept, dept),
            )

    def ask(self, text, plan=None, *, email="victim@example.org", role="employee", dialog="d1", points=POINTS):
        self.n += 1
        model = FakeModel(plan)
        route = Dialog(
            self.dsn,
            model,
            search=lambda *a, **kw: Candidates(list(points), []),
            shortlist=lambda *a, **kw: list(points),
        )
        user = UserContext(email, role, dialog, f"m{self.n}")
        answer = asyncio.run(route.answer(user, text, _status))
        return answer, model, route

    def tickets(self):
        return self.conn.execute("SELECT number,question,user_email FROM tickets ORDER BY id").fetchall()


@pytest.fixture
def env(isolated):
    conn, dsn = isolated
    return Env(conn, dsn)


def claim(source, **extra):
    """Утверждение модели, привязанное к реальному источнику из запроса."""
    data = {"ref": source["ref"], "actor": "", "action": "", "negated": False, "qualifier": "", "object": "",
            "text": source["цитата"]}
    data.update(extra)
    return data


def visible_text(answer):
    """Всё, что пользователь может увидеть в Answer."""
    parts = [answer.summary, answer.caveat or "", answer.profile_note or ""]
    if answer.clarification:
        parts += [answer.clarification.question, *answer.clarification.options]
    parts += [s.text for s in answer.statements]
    for v in answer.variants:
        parts += [v.department, *(s.text for s in v.statements)]
    return "\n".join(parts)
