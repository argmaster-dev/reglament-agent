import asyncio

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.route import Dialog
from reglament_agent.quality.faq import match_published
from reglament_agent.storage.retrieve import Candidates, FAQHit
from tests.dialog.test_quality_rules import Model, hit, seed, status, user


def test_comparison_includes_both_departments_when_first_has_a_matching_faq(isolated):
    conn, dsn = isolated
    first_dept = "Склад Север"
    second_dept = "Склад Восток"
    first_text = "Срок выдачи пропуска — два дня."
    second_text = "Срок выдачи пропуска — пять дней."
    question = "Совпадает ли срок выдачи пропуска у Склада Север и Склада Восток?"
    seed(conn, {"n": (first_dept, [first_text]), "e": (second_dept, [second_text])})

    first = hit(first_dept, first_text, doc="n", number="1.1")
    second = hit(second_dept, second_text, doc="e", number="1.1")
    faq = FAQHit(
        source=SourceRef("n", "FAQ службы качества", first_dept, 1, "", faq_id=41),
        answer=first_text,
        basis=first.source_ref(sentence=0),
        department=first_dept,
        stage="project",
        question="Каков срок выдачи пропуска у склада Север?",
        similarity=0.97,
    )
    assert match_published(question, [faq]) is faq

    def search(conn, question, vector, *, department=None, **kwargs):
        points = [point for point in (first, second) if department in (None, point.department)]
        return Candidates(points, [faq] if department in (None, first_dept) else [])

    route = Dialog(dsn, Model(), search=search, shortlist=lambda *args, **kwargs: [first, second])
    result = asyncio.run(route.answer(user(), question, status))

    assert result.outcome == Outcome.VARIANTS
    assert [variant.department for variant in result.variants] == [first_dept, second_dept]
    for variant, expected in zip(result.variants, (first, second), strict=True):
        assert any(
            statement.text == expected.text and any(ref.doc_id == expected.doc_id for ref in statement.sources)
            for statement in variant.statements
        )
