from reglament_agent.answer import SourceRef, Statement
from reglament_agent.dialog.grounding import Anchor
from reglament_agent.dialog.relevance import conflicting_priority, external_reference, matches_request


def anchor(text, context=""):
    return Anchor(SourceRef("doc", "Положение", "Отдел", 1, "7.2", 0), text, context, 1)


def test_deadline_evidence_has_a_start_and_end():
    query = "За какой срок бронируют помещение?"
    assert matches_request(query, anchor("Бронь оформляют за два дня до встречи."))
    assert not matches_request(query, anchor("Оформляют не позднее следующего дня."))


def test_start_conditions_exclude_a_downstream_reporting_obligation():
    query = "Можно ли приступить к работам?"
    approval = anchor("Начало работ разрешено только при письменном согласовании.")
    later = anchor("Отчёт направляют в течение недели с даты подписания.")
    assert matches_request(query, approval)
    assert not matches_request(query, later)


def test_reference_and_priority_rules_use_the_text_and_not_actor_abbreviations():
    basis = anchor("Расчёт премии выполняют согласно положению о мотивации.")
    assert external_reference(basis)
    assert not external_reference(anchor("Премия равна 5% от суммы."))
    statements = [
        Statement("При обычной просрочке приоритет заявки не меняется.", []),
        Statement("Если согласующий задержал ответ, специалист может понизить приоритет.", []),
    ]
    assert conflicting_priority("Как меняется приоритет?", statements)
    assert not conflicting_priority("Как меняется приоритет?", statements[:1])


def test_companion_sentences_attach_only_directly_adjacent_anaphora():
    from reglament_agent.dialog.grounding import Anchor
    from reglament_agent.dialog.relevance import companion_sentences

    def row(i, text):
        return Anchor(SourceRef("d", "Т", "О", 1, "2.2", i), text, "", 1)

    rows = [row(0, "Лимит согласуют за неделю."), row(1, "Если этот срок нарушен, лимит снижают."),
            row(2, "Для этого она привлекает юриста."), row(3, "Иное не предусмотрено.")]
    rows = [Anchor(SourceRef("d", "Т", "О", 1, "2.2", sentence=i), a.text, "", 1) for i, a in enumerate(rows)]
    middle = Statement(rows[1].text, [rows[1].ref])
    result = companion_sentences([middle], rows)
    assert [s.text for s in result] == [r.text for r in rows[:3]]
    plain = Statement(rows[3].text, [rows[3].ref])
    assert companion_sentences([plain], rows) == [plain]


def test_asks_amount_and_terms_are_form_tolerant():
    from reglament_agent.dialog.relevance import asks_amount, terms

    assert asks_amount("какой будет бонус по сделке?") and asks_amount("чему равен лимит")
    assert not asks_amount("кто назначает бонус")
    assert terms("Согласование командировок") == terms("согласованием командировки")
    assert "кто" not in terms("Кто согласует?")


def test_expand_selected_lists_the_whole_requested_column_in_source_order():
    from reglament_agent.dialog.grounding import Anchor
    from reglament_agent.dialog.relevance import expand_selected

    def row(i, column, text):
        return Anchor(SourceRef("d", "Т", "О", 1, "5.1", sentence=i, column=column), text, "", 1)

    rows = [row(2, "Состав", "Акт"), row(0, "Состав", "Смета"), row(1, "Прочее", "Нет"), row(3, "Состав", "План")]
    chosen = Statement("Состав: Акт", [rows[0].ref])
    items = {"S1": rows[0]}
    expanded = expand_selected("Какие документы входят?", [chosen], items, rows)
    assert [s.text for s in expanded] == ["Состав: Смета", "Состав: Акт", "Состав: План"]
    assert expand_selected("Что входит?", [chosen], items, rows) == [chosen]
