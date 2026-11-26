import asyncio

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from tests.dialog.test_route import Model, status


def test_published_faq_accepts_a_safe_unlisted_paraphrase_but_not_other_intent(isolated):
    conn, dsn = isolated
    model = Model()
    model.result = {"outcome": "no_basis", "claims": []}
    dialog = Dialog(dsn, model)
    conn.execute(
        "INSERT INTO faq_entries(question,answer,basis_kind,author_email,embedding) VALUES (%s,%s,'outside',%s,%s::vector)",
        ("Компенсируют ли такси в региональный выходной?", "Расходы проверяет служба качества.",
         "quality@demo.local", str([0.01] * 1024)),
    )
    other = UserContext("employee@demo.local", "employee", "fresh", "1")
    result = asyncio.run(dialog.answer(other, "Возмещают ли такси в праздник, который выходной только в нашем регионе?", status))
    assert result.outcome == Outcome.EXPERT_ANSWER
    assert result.statements[0].sources[0].faq_id
    assert "quality@demo.local" in result.summary and "в регламентах не закреплено" in result.summary
    assert not model.calls

    different = UserContext("employee@demo.local", "employee", "different", "2")
    refused = asyncio.run(dialog.answer(different, "Кто вызывает такси в региональный выходной?", status))
    assert refused.outcome == Outcome.NO_BASIS and refused.ticket
