from dataclasses import replace

import pytest

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


HEADING = "Инспектор формирует досье поставщика и вкладывает следующие документы."


def cell(sentence, column, text):
    return Anchor(SourceRef("procurement", "Приёмка", "Закупки", 1, "5.2", sentence, column), text, HEADING, 0)


OPTIONAL = cell(2, "При наличии", "Акт лаборатории (заключение экспертизы).")
REQUIRED = cell(2, "Обязательно", "Копия договора поставки.")
ANOTHER = cell(3, "При наличии", "Сведения о транспортировке.")
ROWS = [OPTIONAL, REQUIRED, ANOTHER]


def answer(question, rows=ROWS, selected=(ANOTHER,), outcome="answer", visible=None):
    anchors = {f"S{i}": row for i, row in enumerate(rows if visible is None else visible, 1)}
    claims = [
        {"text": row.text, "ref": model_ref(key, row), "qualifier": row.ref.column}
        for key, row in anchors.items() if row in selected
    ]
    return assemble(question, {"outcome": outcome, "claims": claims}, anchors, ["Закупки"], False, {}, rows)


@pytest.mark.parametrize(("question", "expected"), [
    ("Говорят, без заключения экспертизы досье поставщика не примут. Это верно?", OPTIONAL),
    ("Примут ли досье поставщика без договора поставки?", REQUIRED),
])
def test_unique_named_item_keeps_its_own_requirement_even_if_model_selected_a_neighbour(question, expected):
    result = answer(question)
    assert result.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in result.statements] == [
        (f"{expected.ref.column}: {expected.text}", [expected.ref])
    ]


def test_unqualified_short_name_cannot_choose_between_columns_or_drop_other_conditions():
    duplicate = cell(4, "Обязательно", "Заключение экспертизы для ввоза.")
    result = answer("Примут ли досье поставщика без заключения экспертизы?",
                    rows=ROWS + [duplicate], selected=(OPTIONAL, duplicate))
    assert {s.sources[0] for s in result.statements} == {OPTIONAL.ref, duplicate.ref}


def test_full_column_label_is_preserved():
    qualified = replace(OPTIONAL, ref=replace(OPTIONAL.ref, column="При наличии (для филиала)"))
    result = answer("Примут ли досье поставщика без заключения экспертизы?", rows=[qualified, REQUIRED, ANOTHER])
    assert result.statements[0].text == f"{qualified.ref.column}: {qualified.text}"
    assert result.statements[0].sources == [qualified.ref]


def test_more_specific_missing_item_does_not_match_the_general_item():
    result = answer("Примут ли досье поставщика без заключения экспертизы другого исполнителя?")
    assert OPTIONAL.ref not in [r for s in result.statements for r in s.sources]


@pytest.mark.parametrize("text", ["Договор поставщика.", "Договор на охрану (график поставки)."])
def test_shared_prefix_or_disconnected_words_do_not_identify_a_named_item(text):
    similar = replace(REQUIRED, text=text)
    result = answer("Примут ли досье поставщика без договора поставки?", rows=[OPTIONAL, similar, ANOTHER])
    assert similar.ref not in [r for s in result.statements for r in s.sources]


def test_explicit_parenthetical_alias_can_name_a_document():
    manuals = replace(OPTIONAL, text="Документация аппарата (формуляр, инструкция оператора).")
    result = answer("Примут ли досье поставщика без формуляра аппарата?", rows=[manuals, REQUIRED, ANOTHER])
    assert result.statements[0].sources == [manuals.ref]


def test_several_named_items_keep_their_separate_qualifications():
    question = "Примут ли досье поставщика без заключения экспертизы, договора поставки?"
    result = answer(question, selected=(OPTIONAL, REQUIRED))
    assert {s.sources[0] for s in result.statements} == {OPTIONAL.ref, REQUIRED.ref}


def test_another_process_and_multiple_verified_provisions_do_not_retarget_the_table():
    question = "Примут ли досье поставщика без заключения экспертизы?"
    other = replace(ANOTHER, ref=replace(ANOTHER.ref, point="8.1"))
    result = answer(question, rows=ROWS + [other], selected=(ANOTHER, other))
    assert {s.sources[0] for s in result.statements} == {ANOTHER.ref, other.ref}
    result = answer("Можно ли начать монтаж без заключения экспертизы?")
    assert OPTIONAL.ref not in [r for s in result.statements for r in s.sources]


def test_conditional_exception_and_actor_query_are_preserved():
    exception = cell(8, None, "Досье поставщика для импорта инспектор передаёт руководителю.")
    result = answer("Примут ли досье поставщика без заключения экспертизы?",
                    rows=ROWS + [exception], selected=(ANOTHER, exception))
    assert {s.sources[0] for s in result.statements} == {ANOTHER.ref, exception.ref}
    result = answer("Кто рассматривает досье поставщика без заключения экспертизы?")
    assert OPTIONAL.ref not in [r for s in result.statements for r in s.sources]


def test_no_basis_is_preserved_and_general_list_still_enumerates_its_column():
    result = answer("Примут ли досье поставщика без заключения экспертизы?", selected=(), outcome="no_basis")
    assert result.outcome == Outcome.NO_BASIS and not result.statements
    result = answer("Какие документы при наличии нужны для досье поставщика?")
    assert [s.sources[0] for s in result.statements] == [OPTIONAL.ref, ANOTHER.ref]


def test_named_cell_outside_the_citation_vocabulary_is_not_added():
    result = answer("Примут ли досье поставщика без заключения экспертизы?", visible=[REQUIRED, ANOTHER])
    assert OPTIONAL.ref not in [r for s in result.statements for r in s.sources]
