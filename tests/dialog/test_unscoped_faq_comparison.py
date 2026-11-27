import asyncio

from reglament_agent.answer import Outcome
from reglament_agent.chat.render import assign_chip_names, evidence_groups
from reglament_agent.dialog.route import Dialog
from reglament_agent.ingest.embedding import vector_literal
from tests.dialog.test_quality_rules import Model, TWO, seed, status, user


def test_unscoped_faq_about_the_north_warehouse_does_not_supply_the_east_answer(isolated):
    conn, dsn = isolated
    north = "Склад Север"
    east = "Склад Восток"
    east_rule = "Срок выдачи пропуска — пять дней."
    seed(conn, {"n": (north, ["Срок выдачи пропуска — два дня."]), "e": (east, [east_rule])})
    vector = vector_literal([0.01] * 1024)
    conn.execute("UPDATE points SET embedding=%s::vector, sentences=ARRAY[text]", (vector,))
    conn.execute("UPDATE document_versions SET stats='{\"points\":1}'::jsonb")
    faq_id = conn.execute(
        """INSERT INTO faq_entries (question, answer, basis_kind, author_email, embedding)
           VALUES (%s,%s,'outside','expert@example.org',%s::vector) RETURNING id""",
        ("Каков срок выдачи пропуска у склада Север?",
         "На складе Север пропуск выдают за два дня.", vector),
    ).fetchone()[0]
    answer = asyncio.run(Dialog(dsn, Model()).answer(user(), TWO, status))

    assert answer.outcome == Outcome.VARIANTS
    groups = evidence_groups(answer, assign_chip_names(answer))
    assert [group["label"] for group in groups] == [north, east]
    east_items = groups[1]["items"]
    east_references = [reference["source"] for item in east_items for reference in item["refs"]]
    assert all(ref["faq_id"] != faq_id for ref in east_references), (
        "The same outside-regulations FAQ about North supplies the East row in the rendered comparison"
    )
    assert any(item["text"] == east_rule for item in east_items)
    assert any(ref["doc_id"] == "e" and ref["point"] == "1.1" for ref in east_references)
