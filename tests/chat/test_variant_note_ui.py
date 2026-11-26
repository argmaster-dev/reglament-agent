"""The component that renders the evidence must read the variant note beside non-empty items."""
import re
from pathlib import Path

from reglament_agent.answer import Answer, Outcome, SourceRef, Statement, Variant
from reglament_agent.chat import render as R

COMPONENT = Path(__file__).parents[2] / "public" / "elements" / "AnswerEvidence.jsx"
REF = SourceRef("d", "Т", "Отдел А", 1, "1.1", 0)
FAQ = SourceRef("d", "FAQ", "Отдел А", 1, "", faq_id=5)


def test_attribution_reaches_the_group_data_next_to_the_items():
    attribution = "Ответ службы качества · 01.01.2026 · автор@example.org"
    answer = Answer(Outcome.VARIANTS, variants=[
        Variant("Отдел А", [Statement("Два дня.", [FAQ])], attribution),
        Variant("Отдел Б", [Statement("Пять дней.", [REF])]),
        Variant("Отдел В", [], "в регламентах Отдел В нет основания"),
    ])
    groups = R.evidence_groups(answer, R.assign_chip_names(answer))
    assert groups[0]["items"] and groups[0]["note"] == attribution
    assert groups[1]["note"] is None
    assert not groups[2]["items"] and groups[2]["empty"] == "в регламентах Отдел В нет основания"


def test_component_prints_the_note_only_with_items_and_the_empty_text_only_without():
    source = COMPONENT.read_text(encoding="utf-8")
    guarded = re.search(r"group\.items\.length\s*>\s*0\s*&&\s*group\.note\s*&&|group\.note\s*&&\s*group\.items\.length\s*>\s*0", source)
    assert guarded and source.count("group.note") >= 2
    assert re.search(r"!group\.items\.length\s*&&\s*<p>\{group\.empty\}", source)
