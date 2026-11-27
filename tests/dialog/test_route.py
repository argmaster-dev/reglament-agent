import asyncio
from types import SimpleNamespace

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.grounding import model_ref
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from reglament_agent.storage.sources import Hit


def hit(department, text):
    return Hit(1, department, 1, "4.1", text, 0.04, "Правила", department, 1, [text], [], 0.8, True)


class Model:
    def __init__(self):
        self.calls = []
        self.result = {}

    async def embed(self, texts, **kw):
        return [[0.01] * 1024]

    async def chat_json(self, messages, schema, **kw):
        self.calls.append(messages)
        return SimpleNamespace(data=self.result)


async def status(_):
    pass


def test_dialog_clarifies_before_generation_and_uses_only_real_references(isolated):
    conn, dsn = isolated
    model = Model()
    points = [
        hit("ТД", "За подписание договора отвечает Логист."),
        hit("ДБРЦТ", "Договор подписывает директор."),
    ]

    def search(conn, question, vector, *, department, **kw):
        return Candidates([p for p in points if department in (None, p.department)], [])

    route = Dialog(dsn, model, search=search, shortlist=lambda *args, **kw: points)
    user = UserContext("reader@example.org", "employee", "chat", "1")
    question = "Кто подписывает договор?"
    result = asyncio.run(route.answer(user, question, status))
    assert result.outcome == Outcome.CLARIFY and model.calls == []
    user.message_id = "2"
    model.result = {
        "outcome": "answer",
        "claims": [
            {
                "ref": {"id": "S1", "doc_id": "ТД", "point": "4.1", "sentence": 0},
                "actor": "Логист",
                "action": "подписание",
                "text": "За подписание договора отвечает Логист.",
            },
            {"ref": {"id": "S1", "doc_id": "нет", "point": "9", "sentence": 0}, "text": "Директор."},
        ],
    }
    reply = asyncio.run(route.choose(user, "ТД", status))
    assert reply.outcome == Outcome.ANSWER and reply.caveat
    assert reply.statements[0].sources[0].department == "ТД"
    assert reply.statements[0].sources[0].sentence == 0
    assert asyncio.run(route.choose(user, "ТД", status)) == reply
    assert len(model.calls) == 1
    user.message_id = "3"
    model.result = {
        "outcome": "answer",
        "claims": [
            {"ref": {"id": "S100", "doc_id": "нет", "point": "9", "sentence": 0}, "text": "Чужое основание"}
        ],
    }
    refused = asyncio.run(route.answer(user, "А в ДБРЦТ?", status))
    assert refused.outcome == Outcome.NO_BASIS and refused.ticket
    assert conn.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    assert asyncio.run(route.profiles.get(user.email)).department == "ТД"


@pytest.mark.parametrize("model_outcome", ["off_topic", "low_confidence", "variants"])
def test_indirect_signer_without_a_basis_gets_a_ticket(isolated, model_outcome):
    conn, dsn = isolated
    model = Model()
    wrong = hit(
        "Снабжение",
        "РП инициирует подписание дополнительного соглашения аналогично заключению договора с арендодателем.",
    )
    model.result = {"outcome": model_outcome, "claims": []}
    route = Dialog(dsn, model, search=lambda *args, **kw: Candidates([wrong], []), shortlist=lambda *args, **kw: [])
    user = UserContext("reader@example.org", "employee", "chat", "1")

    answer = asyncio.run(route.answer(user, "Где узнать, на ком подписание договора с арендодателем?", status))

    assert answer.outcome == Outcome.NO_BASIS
    assert answer.statements == []
    assert answer.ticket and answer.ticket.delivery == "pending"
    assert conn.execute(
        "SELECT t.user_email, t.status, m.status FROM tickets t JOIN mail_outbox m ON m.ticket_id=t.id"
    ).fetchone() == (user.email, "open", "pending")


def test_concurrent_retry_and_feedback_are_private_and_idempotent(isolated):
    conn, dsn = isolated
    model = Model()
    question = "Как возместить расходы на переезд?"
    route = Dialog(dsn, model, search=lambda *args, **kw: Candidates([], []))
    model.result = {"outcome": "no_basis", "claims": []}
    user = UserContext("one@example.org", "employee", "thread", "1")

    async def duplicate():
        return await asyncio.gather(
            route.answer(user, question, status), route.answer(user, question, status)
        )

    first, second = asyncio.run(duplicate())
    assert first == second and first.ticket
    assert len(model.calls) == 1
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (1,)
    other = UserContext("two@example.org", "employee", "thread", "1")
    assert asyncio.run(route.rate(other, first.answer_id, False, "Не согласен", True)) is None
    first_dispute = asyncio.run(route.rate(user, first.answer_id, False, "Не учтено условие", True))
    assert first_dispute == asyncio.run(route.rate(user, first.answer_id, False, "Не учтено условие", True))
    assert first_dispute.number != first.ticket.number
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (2,)


def test_published_faq_and_versioned_cache(isolated):
    from reglament_agent.dialog.services import Sources

    conn, dsn = isolated
    model = Model()
    route = Dialog(dsn, model)
    question = "Когда заказывать пропуск?"
    user = UserContext("one@example.org", "employee", "thread", "1")
    conn.execute(
        """INSERT INTO faq_entries(question,alt_questions,answer,basis_kind,author_email,embedding)
                    VALUES (%s,%s,%s,'outside','quality@example.org',%s::vector)""",
        ("Как заказать пропуск?", [question], "За день до визита.", str([0.01] * 1024)),
    )
    answer = asyncio.run(route.answer(user, question, status))
    assert answer.outcome == Outcome.EXPERT_ANSWER and not model.calls
    ref = answer.statements[0].sources[0]
    assert asyncio.run(Sources(route.profiles).point(ref)).sentences == ["За день до визита."]
    user.message_id = "2"
    assert asyncio.run(route.answer(user, question, status)).from_cache
    conn.execute("UPDATE faq_entries SET status='review'")
    model.result = {"outcome": "no_basis", "claims": []}
    user.message_id = "3"
    after = asyncio.run(route.answer(user, question, status))
    assert not after.from_cache and after.ticket


def test_distinguishes_signed_artifact_and_requires_actual_table_column():
    from dataclasses import replace

    from reglament_agent.dialog.grounding import anchors, assemble, vocabulary
    from reglament_agent.ingest.model import Block, BlockKind, Table

    question = "Кто подписывает договор с подрядчиком?"
    wrong = hit("Снабжение", "РП организует подписание УПД с подрядчиком.")
    right = hit("Снабжение", "За подписание договора с подрядчиком отвечает Логист.")
    assert [a.text for a in vocabulary(question, anchors([wrong, right])).values()] == [right.text]
    table = Table(["Обязательно", "При наличии"], [["Расчёт", "Схема"], ["План", ""]])
    table_hit = replace(
        right,
        sentences=[
            "Карточка проекта требует приложения документов.",
            "Обязательно: Расчёт | При наличии: Схема",
            "Обязательно: План",
        ],
        blocks=[Block(BlockKind.TABLE, "", table)],
    )
    question = "Какие документы обязательны для карточки проекта?"
    rows = anchors([table_hit])
    items = vocabulary(question, rows)
    ref = next(k for k, a in items.items() if a.ref.column == "Обязательно")
    optional = next(k for k, a in items.items() if a.ref.column == "При наличии")
    data = {
        "outcome": "answer",
        "claims": [
            {"ref": model_ref(ref, items[ref]), "text": "Расчёт", "qualifier": "Обязательно"},
            {"ref": model_ref(optional, items[optional]), "text": "Схема", "qualifier": "Обязательно"},
        ],
    }
    trace = {}
    answer = assemble(question, data, items, [], False, trace, rows)
    assert [s.text for s in answer.statements] == ["Обязательно: Расчёт", "Обязательно: План"]
    assert trace["dropped"] == ["column_mismatch"] and answer.caveat


def test_exhausted_clarification_carries_transcript(isolated):
    conn, dsn = isolated
    model = Model()
    points = [
        hit("Отдел А", "За подписание договора отвечает Логист."),
        hit("Отдел Б", "Договор подписывает директор."),
    ]
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: points)
    user = UserContext("test@example.org", "employee", "thread", "1")
    assert asyncio.run(route.answer(user, "Кто подписывает договор?", status)).outcome == Outcome.CLARIFY
    for i in range(3):
        user.message_id = str(i + 2)
        answer = asyncio.run(route.answer(user, "не помню название", status))
    assert answer.ticket and not model.calls
    reason, history = conn.execute("SELECT reason,clarifications FROM tickets").fetchone()
    assert reason == "clarify_exhausted" and len(history) == 3
    retry = asyncio.run(route.answer(user, "не помню название", status))
    assert retry.ticket.number == answer.ticket.number
    assert conn.execute("SELECT count(*) FROM tickets").fetchone() == (1,)


def test_unsupported_actor_is_not_rendered(isolated):
    conn, dsn = isolated
    model = Model()
    points = [hit("ТД", "За подписание договора с подрядчиком отвечает Логист.")]
    route = Dialog(
        dsn, model, search=lambda *a, **k: Candidates(points, []), shortlist=lambda *a, **k: points
    )
    model.result = {
        "outcome": "answer",
        "claims": [
            {
                "ref": {"id": "S1", "doc_id": "ТД", "point": "4.1", "sentence": 0},
                "text": "Договор подписывает РП.",
                "actor": "РП",
                "action": "подписывает",
            }
        ],
    }
    reply = asyncio.run(
        route.answer(
            UserContext("one@example.org", "employee", "one", "01"),
            "Кто подписывает договор с подрядчиком?",
            status,
        )
    )
    assert reply.outcome == Outcome.NO_BASIS and not reply.statements and reply.ticket
    trace = conn.execute("SELECT trace FROM dialog_requests WHERE id=%s", (reply.answer_id,)).fetchone()[0]
    assert "actor_missing" in trace["dropped"]


def test_variants_keep_distinct_deadlines_and_absent_department(isolated):
    import json

    _, dsn = isolated
    rows = [
        hit("Управление А", "Заявку нужно подать за 3 дня до начала работ."),
        hit("Управление Б", "Расчёт нужен за 2 недели до подачи предложения."),
        hit("Управление В", "Заявки принимает специалист."),
    ]

    class Select(Model):
        async def chat_json(self, messages, schema, **kw):
            self.calls.append(messages)
            docs = json.loads(messages[1]["content"])["источники"]
            return SimpleNamespace(
                data={"outcome": "variants", "claims": [{"ref": v["ref"], "text": v["цитата"]} for v in docs]}
            )

    route = Dialog(dsn, Select(), search=lambda *a, **k: Candidates(rows, []), shortlist=lambda *a, **k: rows)
    user = UserContext("user@example.org", "employee", "thread", "1")
    first = asyncio.run(route.answer(user, "За сколько подавать заявку?", status))
    assert first.outcome == Outcome.CLARIFY and not route.model.calls
    user.message_id = "2"
    reply = asyncio.run(route.choose(user, "Не знаю", status))
    variants = {v.department: [s.text for s in v.statements] for v in reply.variants}
    assert variants == {"Управление А": [rows[0].text], "Управление Б": [rows[1].text], "Управление В": []}
    assert not asyncio.run(route.profiles.get(user.email)).department


def test_retry_after_model_error_and_glossary(isolated):
    from reglament_agent.model.ollama import ModelUnavailable

    conn, dsn = isolated

    class InitiallyUnavailable(Model):
        async def chat_json(self, *a, **kw):
            if not self.calls:
                self.calls.append(None)
                raise ModelUnavailable("unavailable")
            return SimpleNamespace(data={"outcome": "off_topic", "claims": []})

    route = Dialog(dsn, InitiallyUnavailable())
    user = UserContext("foo@example.org", "employee", "dialog", "1")
    first = asyncio.run(route.answer(user, "Спой песню", status))
    assert first.outcome == Outcome.ERROR and not first.ticket
    again = asyncio.run(route.answer(user, "Спой песню", status))
    assert again.outcome == Outcome.OFF_TOPIC and not again.ticket
    assert conn.execute("SELECT count(*) FROM dialog_requests").fetchone() == (1,)
    user.message_id = "2"
    definition = asyncio.run(route.answer(user, "Что такое ТКП?", status))
    assert definition.outcome == Outcome.ANSWER and "предложение" in definition.summary.lower()


def test_escalation_reasons_match_delivery(isolated):
    import json

    conn, dsn = isolated
    points = [
        hit("Сервис", "Заявки проверяются ежедневно согласно регламенту обслуживания."),
        hit("Сервис", "Заявки проверяются только по понедельникам."),
    ]

    class Select(Model):
        async def chat_json(self, messages, schema, **kw):
            docs = json.loads(messages[1]["content"])["источники"]
            return SimpleNamespace(
                data={
                    "outcome": self.outcome,
                    "claims": [{"ref": v["ref"], "text": v["цитата"]} for v in docs],
                }
            )

    model = Select()
    route = Dialog(
        dsn, model, search=lambda *a, **k: Candidates(points, []), shortlist=lambda *a, **k: points
    )
    for reason in ("external_doc", "conflict", "low_confidence"):
        model.outcome = reason
        user = UserContext("foo@example.org", "employee", "scope", reason)
        result = asyncio.run(route.answer(user, "Когда проверяются заявки?", status))
        assert result.outcome == reason and result.ticket
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (3,)


def test_product_entrypoint_registers_health_and_uses_product_database(isolated, monkeypatch, tmp_path):
    import subprocess
    import sys
    from textwrap import dedent

    _, dsn = isolated
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAT_SERVICES_FACTORY", "reglament_agent.dialog.services:build_services")
    # Startup configuration is read once per process, including during an ordinary server restart.
    check = dedent("""
        from reglament_agent.chat import app
        from reglament_agent.dialog.route import Dialog

        assert isinstance(app.services.answerer, Dialog)
        assert app.channel_storage() is None
        health = next(r for r in app.fastapi_app.router.routes if getattr(r, "path", "") == "/health")
        assert health.endpoint.__module__ == "reglament_agent.ops.health"
    """)
    subprocess.run([sys.executable, "-c", check], check=True)


def test_user_scope_is_not_taken_from_another_conversation(isolated):
    _, dsn = isolated
    model = Model()
    original = ["За сколько подавать заявку?", "Кто подписывает договор?"]
    notes = []

    def search(conn, question, vector, **kw):
        notes.append((question, kw["department"]))
        return Candidates([], [])

    route = Dialog(dsn, model, search=search, shortlist=lambda *a, **kw: [])
    model.result = {"outcome": "no_basis", "claims": []}
    users = [UserContext("same@example.org", "employee", str(i), "1") for i in range(2)]
    # Departments are obtained from active document cards in production.
    from pathlib import Path

    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.ingest.parser import load_cards
    from reglament_agent.storage.publish import publish

    conn = isolated[0]
    card = next(
        c for c in load_cards(Path(__file__).resolve().parents[2] / "docx") if c.department == "Дирекция ИБ"
    )
    publish(
        conn,
        card,
        (Path(__file__).resolve().parents[2] / "docx" / card.file).read_bytes(),
        FakeEmbedder(),
        "test",
    )
    for user, question in zip(users, original):
        asyncio.run(route.answer(user, question, status))
    for user, question in zip(users, original):
        user.message_id = "2"
        asyncio.run(route.answer(user, "А в Дирекции ИБ?", status))
        assert notes[-1] == (question, "Дирекция ИБ")
    assert asyncio.run(route.profiles.get(users[0].email)).department is None


def test_swapped_column_from_generator_escalates_without_publishing_it(isolated):
    import json
    from dataclasses import replace

    from reglament_agent.ingest.model import Block, BlockKind, Table

    conn, dsn = isolated
    cells = Table(["Обязательно", "При наличии"], [["Акт", "Пояснительная записка"]])
    source = replace(
        hit("Отдел", "Карточка проекта требует документов."),
        sentences=[
            "Карточка проекта требует документов.",
            "Обязательно: Акт | При наличии: Пояснительная записка",
        ],
        blocks=[Block(BlockKind.TABLE, "", cells)],
    )

    class SwappedColumn(Model):
        async def chat_json(self, messages, schema, **kw):
            rows = json.loads(messages[1]["content"])["источники"]
            optional = next(row for row in rows if row["колонка"] == "При наличии")
            return SimpleNamespace(
                data={
                    "outcome": "answer",
                    "claims": [
                        {
                            "ref": optional["ref"],
                            "qualifier": "Обязательно",
                            "text": "Пояснительная записка обязательна.",
                        }
                    ],
                }
            )

    route = Dialog(dsn, SwappedColumn(), search=lambda *a, **kw: Candidates([source], []))
    result = asyncio.run(
        route.answer(
            UserContext("user@example.org", "employee", "dlg", "1"),
            "Какие документы обязательны для карточки проекта?",
            status,
        )
    )
    assert result.outcome == Outcome.NO_BASIS and result.ticket and not result.statements
    trace = conn.execute("SELECT trace FROM dialog_requests WHERE id=%s", (result.answer_id,)).fetchone()[0]
    assert "column_mismatch" in trace["dropped"]


def test_permission_to_start_retains_alternative_conditions_and_their_scope():
    from dataclasses import replace

    from reglament_agent.dialog.grounding import anchors, assemble, vocabulary

    question = "Можно ли начать работы без договора?"
    intro = "Исполнение проекта начинается при выполнении одного из условий:"
    rule = replace(
        hit("Отдел", intro), sentences=[intro, "подписан договор;", "получено разрешение владельца."]
    )
    later = replace(
        hit("Отдел", "Не позднее трёх дней с даты подписания договора оператор создаёт заказ."), number="6.2"
    )
    rows = anchors([later, rule])
    items = vocabulary(question, rows)
    assert all(a.ref.point != "6.2" for a in items.values())
    k = next(k for k, a in items.items() if "разрешение" in a.text)
    result = assemble(
        question,
        {"outcome": "answer", "claims": [{"ref": model_ref(k, items[k]), "text": items[k].text}]},
        items,
        ["Отдел"],
        False,
        {},
        rows,
    )
    assert result.outcome == Outcome.ANSWER and len(result.statements) == 3
    assert [s.sources[0].sentence for s in result.statements] == [0, 1, 2]


def test_dialog_remembers_explicit_position_without_affecting_scope(isolated):
    _, dsn = isolated
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    route = Dialog(dsn, model, search=lambda *args, **kw: Candidates([], []))
    user = UserContext("profile@example.org", "employee", "chat", "position")
    asyncio.run(route.profiles.set_department(user.email, "Отдел А"))
    reply = asyncio.run(route.answer(user, "Моя должность — логист.", status))
    assert reply.outcome == Outcome.ANSWER and "логист" in reply.summary
    profile = asyncio.run(route.profiles.get(user.email))
    assert profile.department == "Отдел А" and profile.position == "логист"
    asyncio.run(route.profiles.set_department(user.email, "Отдел Б"))
    assert asyncio.run(route.profiles.get(user.email)).position == "логист"
    asyncio.run(route.profiles.reset(user.email))
    cleared = asyncio.run(route.profiles.get(user.email))
    assert (cleared.department, cleared.position, cleared.confirmed_at) == (None, None, None)
    assert model.calls == []


def test_department_follow_up_during_clarification_is_temporary(isolated):
    conn, dsn = isolated
    from reglament_agent.storage.publish import publish
    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.ingest.parser import load_cards
    from pathlib import Path

    docs = Path(__file__).resolve().parents[2] / "docx"
    for card in load_cards(docs):
        publish(conn, card, (docs / card.file).read_bytes(), FakeEmbedder(), "test")
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    points = [
        hit("ДБРЦТ", "Договор подписывает директор."),
        hit("Техническая дирекция", "За подписание договора отвечает Логист."),
    ]
    notes = []

    def search(conn, question, vector, *, department, **kw):
        notes.append((question, department))
        return Candidates([p for p in points if p.department == department], [])

    route = Dialog(dsn, model, search=search, shortlist=lambda *args, **kw: points)
    user = UserContext("profile@example.org", "employee", "chat", "before")
    question = "Кто подписывает договор?"
    assert asyncio.run(route.answer(user, question, status)).outcome == Outcome.CLARIFY
    user.message_id = "temporary"
    asyncio.run(route.choose(user, "А в Технической дирекции?", status))
    assert notes[-1] == (question, "Техническая дирекция")
    assert asyncio.run(route.profiles.get(user.email)).department is None
    user.message_id = "again"
    assert asyncio.run(route.answer(user, question, status)).outcome == Outcome.CLARIFY
    user.message_id = "personal"
    asyncio.run(route.choose(user, "ДБРЦТ", status))
    assert asyncio.run(route.profiles.get(user.email)).department == "ДБРЦТ"


def test_database_connection_failure_returns_retryable_error(isolated, monkeypatch):
    import psycopg

    _, dsn = isolated
    route = Dialog(dsn, Model())
    user = UserContext("reader@example.org", "employee", "chat", "unavailable")

    def unavailable(*args, **kw):
        raise psycopg.OperationalError("connection refused")

    with monkeypatch.context() as faults:
        faults.setattr(psycopg, "connect", unavailable)
        answer = asyncio.run(route.answer(user, "Что означает РП?", status))
    assert answer.outcome == Outcome.ERROR and answer.answer_id is None
    reply = asyncio.run(route.answer(user, "Что означает РП?", status))
    assert reply.outcome == Outcome.ANSWER and reply.answer_id
    assert asyncio.run(route.answer(user, "Что означает РП?", status)) == reply


def test_stale_department_is_confirmed_once_per_new_dialog(isolated, monkeypatch):
    conn, dsn = isolated
    monkeypatch.setenv("DIALOG_PROFILE_MAX_AGE_DAYS", "1")
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    route = Dialog(dsn, model, search=lambda *args, **kw: Candidates([], []))
    user = UserContext("stale@example.org", "employee", "new chat", "ask")
    asyncio.run(route.profiles.set_department(user.email, "Отдел А"))
    conn.execute("UPDATE dialog_profiles SET confirmed_at=now() - interval '2 days'")
    result = asyncio.run(route.answer(user, "Кто утверждает график?", status))
    assert result.outcome == Outcome.CLARIFY
    assert "по-прежнему" in result.clarification.question and "Да" in result.clarification.options
    user.message_id = "confirm"
    reply = asyncio.run(route.choose(user, "Да", status))
    assert reply.scope.department == "Отдел А" and "Подтвердил" in reply.profile_note
    assert asyncio.run(route.profiles.get(user.email)).confirmed_at is not None
    user.message_id = "ask-again"
    reply = asyncio.run(route.answer(user, "Кто утверждает договор?", status))
    assert reply.outcome != Outcome.CLARIFY


def test_a_lost_commit_ack_recovers_the_same_ticket(isolated, monkeypatch):
    import psycopg

    conn, dsn = isolated
    connect = psycopg.connect
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    route = Dialog(dsn, model, search=lambda *args, **kwargs: Candidates([], []))
    user = UserContext("reader@example.org", "employee", "chat", "ticket")

    class LostCommitAck:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            self.connection.commit()
            raise psycopg.OperationalError("lost connection after commit")

    with monkeypatch.context() as faults:
        faults.setattr(psycopg, "connect", lambda dsn: LostCommitAck(connect(dsn)))
        result = asyncio.run(route.answer(user, "Какие документы нужны?", status))
    assert result.outcome == Outcome.ERROR
    answer = asyncio.run(route.answer(user, "Какие документы нужны?", status))
    assert answer.ticket and model.calls and len(model.calls) == 1
    assert conn.execute("SELECT count(*) FROM tickets").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (1,)


def test_identical_applicable_rules_do_not_force_department_clarification(isolated):
    _, dsn = isolated
    points = [hit(department, "Договор подписывает директор.") for department in ("Отдел А", "Отдел Б")]
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    route = Dialog(
        dsn,
        model,
        shortlist=lambda *args, **kwargs: points,
        search=lambda *args, **kwargs: Candidates(points, []),
    )
    user = UserContext("person@example.org", "employee", "chat", "common-rule")
    result = asyncio.run(route.answer(user, "Кто подписывает договор?", status))
    assert result.outcome != Outcome.CLARIFY and len(model.calls) == 1


def test_model_failure_does_not_consume_clarification_or_save_profile(isolated):
    from reglament_agent.model.ollama import ModelUnavailable

    conn, dsn = isolated
    points = [
        hit("Отдел А", "Договор подписывает директор."),
        hit("Отдел Б", "За подписание договора отвечает Логист."),
    ]

    class OnceUnavailable(Model):
        async def chat_json(self, *args, **kwargs):
            if not self.calls:
                self.calls.append(None)
                raise ModelUnavailable("unavailable")
            return SimpleNamespace(data={"outcome": "no_basis", "claims": []})

    route = Dialog(
        dsn,
        OnceUnavailable(),
        shortlist=lambda *args, **kwargs: points,
        search=lambda *args, **kwargs: Candidates([], []),
    )
    user = UserContext("reader@example.org", "employee", "chat", "question")
    assert asyncio.run(route.answer(user, "Кто подписывает договор?", status)).outcome == Outcome.CLARIFY
    user.message_id = "choice"
    assert asyncio.run(route.choose(user, "Отдел А", status)).outcome == Outcome.ERROR
    assert asyncio.run(route.profiles.get(user.email)).department is None
    topic = conn.execute("SELECT state FROM dialog_topics").fetchone()[0]
    assert "history" not in topic and topic["options"] == ["Отдел А", "Отдел Б"]
    assert asyncio.run(route.choose(user, "Отдел А", status)).ticket
    assert asyncio.run(route.profiles.get(user.email)).department == "Отдел А"
    assert conn.execute("SELECT count(*) FROM tickets").fetchone() == (1,)


def test_stage_clarification_preserves_the_scoped_department(isolated):
    from dataclasses import replace
    from pathlib import Path

    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.ingest.parser import load_cards
    from reglament_agent.storage.publish import publish

    conn, dsn = isolated
    docs = Path(__file__).resolve().parents[2] / "docx"
    department = "Дирекция ИБ"
    for card in load_cards(docs):
        if card.department == department:
            publish(conn, card, (docs / card.file).read_bytes(), FakeEmbedder(), "test")
    points = [
        replace(hit(department, "Начальник контролирует сроки."), doc_id="ib-project"),
        replace(hit(department, "Куратор контролирует сроки."), doc_id="ib-presale"),
    ]
    checked = []

    def search(conn, question, vector, *, department, stage, **kwargs):
        checked.append((question, department, stage))
        return Candidates([], [])

    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    route = Dialog(dsn, model, search=search, shortlist=lambda *args, **kwargs: points)
    user = UserContext("stage@example.org", "employee", "chat", "question")
    question = "Кто контролирует сроки в Дирекции ИБ?"
    request = asyncio.run(route.answer(user, question, status))
    assert request.outcome == Outcome.CLARIFY and "Пресейл" in request.clarification.options
    user.message_id = "choose"
    result = asyncio.run(route.choose(user, "Пресейл", status))
    assert result.scope.stage == "presale" and checked[-1] == (question, department, "presale")
    assert asyncio.run(route.profiles.get(user.email)).department is None
