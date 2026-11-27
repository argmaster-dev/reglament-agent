"""Deadline wording and regulatory context must survive evidence selection."""
from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding


def anchor(number, sentence, index=0, context="", rank=0, section_title=""):
    return grounding.Anchor(
        SourceRef("sample", "Инструкция", "Отдел", 1, number, index),
        sentence, context, rank, section_title,
    )


def proposal(key, item):
    return {"outcome": "answer", "claims": [{"ref": grounding.model_ref(key, item), "text": item.text}]}


def test_deadline_accepts_temporal_sentence_with_subject_elsewhere_in_point():
    question = "Сколько дней отводится на подачу заявления о командировке?"
    correct = anchor("3.4", "Подготовку начинают за пять рабочих дней до отъезда.", 2,
                     "Потребность в поездке согласует руководитель.", rank=1)
    subject = anchor("3.4", "Сотрудник подаёт заявление о командировке.", 1, rank=1)
    unrelated = anchor("7.8", "После подачи заявления о командировке руководитель назначает исполнителя.")
    rows = [unrelated, correct, subject]

    items = grounding.vocabulary(question, rows)
    key = next(key for key, item in items.items() if item == correct)
    result = grounding.assemble(question, proposal(key, correct), items, ["Отдел"], False, {}, rows)

    assert result.outcome == Outcome.ANSWER
    assert [st.sources[0] for st in result.statements] == [correct.ref]
    assert unrelated not in items.values()


def test_explicit_time_relation_and_named_role_exclude_neighbouring_rules():
    question = "Сколько рабочего времени у ПА после назначения, чтобы принять заявку?"
    right = anchor("3.4", "ПА обязан принять заявку в течение одного рабочего дня с момента назначения.")
    before = anchor("3.5", "ПА должен принять заявку за два рабочих дня до назначения.")
    other_role = anchor("3.6", "РМ должен принять заявку в течение двух дней после назначения.",
                        context="ПА участвует в других этапах.")
    other_action = anchor("3.7", "ПА проверяет заявку в течение трёх дней после её принятия.")
    items = grounding.vocabulary("Уточните, " + question.lower(), [right, before, other_role, other_action])
    assert list(items.values()) == [right]


def test_deadline_uses_the_owner_of_the_timed_action_not_the_recipient_of_a_later_delivery():
    from reglament_agent.dialog import relevance

    question = "За сколько дней до передачи отчёта заказчику оформляют заявку на аудит?"
    first = anchor("4.1", "Заявку на аудит оформляют за три рабочих дня до передачи отчёта.")
    second = anchor("4.2", "Заявку на аудит оформляют за пять дней до передачи отчёта заказчику.")
    assert relevance.applicable_refs(question, [first, second]) == {first.ref, second.ref}
    assert not relevance._roles("до передачи отчёта заказчику")
    assert not relevance._roles("до передачи отчёта заказчику оформляют заявку")
    owner = relevance._roles("ПА должен оформить заявку")
    assert owner
    assert relevance._roles("До передачи отчёта заказчику ПА должен оформить заявку") == owner
    assert relevance._roles("После передачи отчёта заказчику у ПА есть два дня") == owner


def test_deadline_lookalike_in_a_section_cannot_supply_a_different_operation():
    question = "Какой срок подачи заявления на отпуск?"
    right = anchor("4.1", "Работник подаёт заявление на отпуск за пять дней до поездки.")
    section = "Подача заявления на отпуск"
    other_action = anchor("4.2", "Сотрудник проверяет заявление на отпуск в течение двух дней после подачи.",
                          section_title=section)
    other_subject = anchor("4.3", "Путевой лист закрывают в течение двух дней после рейса.",
                           section_title=section)
    rows = [right, other_action, other_subject]
    assert list(grounding.vocabulary(question, rows).values()) == [right]
    rejected = grounding.assemble(question, proposal("S1", other_action), {"S1": other_action},
                                  ["Отдел"], False, {}, rows)
    assert rejected.outcome == Outcome.NO_BASIS and not rejected.statements


def test_responsible_person_is_not_a_deadline_request():
    from reglament_agent.dialog.relevance import asks_deadline

    assert not asks_deadline("Кто отвечает за сроки рассмотрения заявления?")
    assert not asks_deadline("Что произойдёт при нарушении сроков подачи заявления?")


def test_deadline_flows_from_scope_to_citations_in_the_indexed_corpus(isolated):
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    from reglament_agent.answer import UserContext
    from reglament_agent.dialog import state
    from reglament_agent.dialog.route import Dialog
    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.ingest.parser import load_cards
    from reglament_agent.storage.publish import publish

    conn, dsn = isolated
    corpus = Path(__file__).resolve().parents[2] / "docx"
    embedder = FakeEmbedder()
    for card in load_cards(corpus):
        publish(conn, card, (corpus / card.file).read_bytes(), embedder, "deadline-regression")

    class Selector:
        expected = None

        async def embed(self, texts, **kwargs):
            return embedder.embed(texts)

        async def chat_json(self, messages, schema, **kwargs):
            refs = schema["properties"]["claims"]["items"]["properties"]["ref"]["enum"]
            matches = [ref for ref in refs if (ref["doc_id"], ref["point"]) == self.expected]
            assert matches, f"Expected {self.expected} missing from cited choices: {refs}"
            if self.expected[0] == "td-project":
                assert not any(ref["doc_id"] == "td-project" and ref["point"] == "2.1" for ref in refs), refs
            return SimpleNamespace(data={"outcome": "answer", "claims": [{"ref": matches[0]}]})

    async def status(_):
        pass

    async def cases():
        questions = (
            "За сколько подавать заявку на пресейл?",
            "Какой срок подачи заявки на пресейл?",
            "За сколько дней до подачи предложения заказчику следует инициировать заявку на пресейл?",
            "Сколько дней даётся на подачу заявки на пресейл?",
            "В течение какого времени подаётся заявка на пресейл?",
            "Подскажите, сколько дней отводится на подачу пресейл-заявки?",
        )
        model = Selector()
        dialog = Dialog(dsn, model)
        scopes = (("Техническая дирекция", "td-project"), ("ДБРЦТ", "dbrct-project"))
        for index, question in enumerate(questions):
            for department, doc in scopes:
                user = UserContext(f"u{index}-{doc}@example.test", "employee", "deadline", "start")
                model.expected = (doc, "1.5")
                first = await dialog.answer(user, question, status)
                assert first.outcome == Outcome.CLARIFY, (question, first.outcome, first.summary)
                assert department in first.clarification.options
                user.message_id = "department"
                answer = await dialog.choose(user, department, status)
                assert answer.outcome == Outcome.ANSWER and answer.scope.department == department
                assert {(ref.doc_id, ref.point) for st in answer.statements for ref in st.sources} == {model.expected}
                assert answer.ticket is None

        user = UserContext("g30@example.test", "employee", "deadline", "pa")
        await dialog.profiles.run(lambda database: state.update_profile(database, user.email, department="Дирекция ИБ"))
        model.expected = ("ib-presale", "5.1")
        answer = await dialog.answer(user, "Сколько рабочего времени у ПА после назначения, чтобы принять заявку?", status)
        assert answer.outcome == Outcome.ANSWER, (answer.outcome, answer.summary)
        assert "в течение 1 рабочего дня с момента назначения" in answer.summary
        assert {(ref.doc_id, ref.point) for st in answer.statements for ref in st.sources} == {model.expected}

    asyncio.run(cases())
