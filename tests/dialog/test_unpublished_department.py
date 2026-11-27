"""An explicitly named unit with no published regulation cannot borrow another unit's answer."""
import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog import context, state
from reglament_agent.dialog.route import Dialog
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.storage.publish import publish
from tests.admin.test_versions_summary import docx
from tests.dialog.test_quality_rules import status
from tests.dialog.test_request_scope import FirstSource


@pytest.mark.parametrize(("label", "profile", "published_name"), [
    ("Технической дирекции", None, "Техническая дирекция"),
    ("ТД", "Дирекция ИБ", "Техническая дирекция"),
    ("Технической дирекции", "Дирекция ИБ", "ТД"),
])
def test_requested_department_before_and_after_publication(isolated, label, profile, published_name):
    conn, dsn = isolated
    def publish_for(doc_id, department, rule):
        card = DocumentCard(doc_id, "Порядок заключения договоров", department, ("project",), "manual.docx")
        publish(conn, card, docx(card.title, "1. Договоры", "1.1. " + rule), FakeEmbedder(), "expert")

    publish_for("ib", "Дирекция ИБ", "Договор с субподрядчиком подписывает руководитель проекта.")
    employee = "employee@example.org"
    if profile:
        state.set_department(conn, employee, profile)
    route = Dialog(dsn, FirstSource())

    def ask(question, message_id, dialog="contract"):
        user = UserContext(employee, "employee", dialog, message_id)
        return asyncio.run(route.answer(user, question, status))

    base = "Кто подписывает договор с субподрядчиком"
    scoped = f"{base} в {label}?"
    before = ask(scoped, "before")
    assert before.outcome == Outcome.NO_BASIS and before.ticket
    assert not before.statements and before.scope.department == "Техническая дирекция"
    assert "Техническая дирекция" in before.summary
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (1,)
    assert not route.model.calls
    assert conn.execute("SELECT cache_key FROM dialog_requests WHERE id=%s", (before.answer_id,)).fetchone() == (None,)

    unscoped = ask(base + "?", "regular", "control")
    assert unscoped.outcome == Outcome.ANSWER
    assert {r.doc_id for s in unscoped.statements for r in s.sources} == {"ib"}
    definition = ask("Что такое ТЗ?", "term", "definition")
    assert definition.outcome == Outcome.ANSWER and "Техническое задание" in definition.summary

    publish_for("td", published_name, "Договор с субподрядчиком подписывает логист.")
    after = ask(scoped, "after")
    assert after.outcome == Outcome.ANSWER and not after.ticket
    assert {r.doc_id for s in after.statements for r in s.sources} == {"td"}
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (1,)


def test_named_units_only_restrict_when_used_as_scope():
    catalog = {"Техническая дирекция", "Дирекция ИБ"}
    assert context.scoped_departments_in("Что означает ТД?", catalog) == []
    assert context.scoped_departments_in("Кто согласует ТД?", catalog) == []
    assert context.scoped_departments_in("Как указать ИБ в ТЗ?", catalog) == []
    assert context.scoped_departments_in("Кто в ТД согласует договор с Дирекцией ИБ?", catalog) == ["Техническая дирекция"]
    assert context.scoped_departments_in("Кто отвечает в ТД и ИБ?", catalog) == ["Техническая дирекция", "Дирекция ИБ"]


@pytest.mark.parametrize("td_published", [False, True])
def test_employee_supplied_expansion_controls_scope(isolated, td_published):
    conn, dsn = isolated
    departments = ["Дирекция ИБ", "Техническая дирекция"] if td_published else ["Дирекция ИБ"]
    for index, department in enumerate(departments):
        card = DocumentCard(f"manual-{index}", "Учёт оборудования", department, ("project",), "manual.docx")
        publish(conn, card, docx(card.title, "1. Учёт", "1.1. Оборудование учитывают в штуках."),
                FakeEmbedder(), "expert")
    route = Dialog(dsn, FirstSource())
    subject = "Какие единицы измерения указать в ТД (технической документации) на оборудование"
    for index, (question, expected) in enumerate([
        (subject + "?", None),
        (subject + " для Дирекции ИБ?", "Дирекция ИБ"),
        ("Кто выдаёт оборудование в ТД (Технической дирекции)?", "Техническая дирекция"),
    ]):
        user = UserContext("employee@example.org", "employee", f"expansion-{index}", str(index))
        result = asyncio.run(route.answer(user, question, status))
        assert result.scope.department == expected
        steps, = conn.execute("SELECT trace->'steps' FROM dialog_requests WHERE id=%s", (result.answer_id,)).fetchone()
        assert ("unpublished_department" in steps) == (expected == "Техническая дирекция" and not td_published)
