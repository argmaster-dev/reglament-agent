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
