from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


def provision(point, *sentences, department="Снабжение", title="Порядок работ"):
    return [Anchor(SourceRef("rules-" + department, "Правила", department, 2, point, n),
                   text, sentences[0] if n else "", 0, title) for n, text in enumerate(sentences)]


def answer(question, selected):
    items = {f"S{i}": row for i, row in enumerate(selected, 1)}
    claims = [{"ref": model_ref(key, row), "text": row.text} for key, row in items.items()]
    return assemble(question, {"outcome": "answer", "claims": claims}, items,
                    ["Снабжение", "Логистика"], False, {}, selected)


def texts(result):
    return [(s.text, s.sources[0].point) for s in result.statements]


def test_requested_equivalent_needs_its_own_rule_and_requested_procurement_condition():
    rule = provision("2", "При наличии эквивалента детали необходимо согласовать закупку с клиентом.",
                     "Для государственных закупок требуется обоснование замены.")
    late = provision("4", "Если отгрузка просрочена, директор согласовывает закупку эквивалента детали.")
    other = provision("3", "Закупка аналога согласуется с инженером.", department="Логистика")
    result = answer("Для госзакупки можно поставить эквивалент снятой с производства детали?", late + other + rule)
    assert result.outcome == Outcome.ANSWER and texts(result) == [(a.text, "2") for a in rule]


def test_conditional_replacement_is_not_evidence_without_its_trigger_or_special_terms():
    special = provision("7", "Если отгрузка просрочена, директор согласовывает закупку эквивалента детали.")
    general = provision("2", "Закупка эквивалента детали согласуется с клиентом.")
    assert answer("Можно поставить эквивалент детали?", special).outcome == Outcome.NO_BASIS
    assert answer("Отгрузка не просрочена, можно поставить эквивалент детали?", special).outcome == Outcome.NO_BASIS
    applied = answer("Отгрузка просрочена, можно поставить эквивалент детали?", special)
    assert applied.outcome == Outcome.ANSWER and texts(applied) == [(special[0].text, "7")]
    deferred = provision("8", "Закупка эквивалента допускается только при задержке поставки.")
    assert answer("Можно поставить эквивалент детали?", deferred).outcome == Outcome.NO_BASIS
    assert answer("При госзакупке можно поставить эквивалент детали?", general).outcome == Outcome.NO_BASIS


def test_escalate_status_change_keeps_the_primary_instruction_before_its_follow_up():
    rule = provision("2", "При переходе проекта в красную зону куратор эскалирует проблему дежурному.",
                     "В письме куратор указывает причины изменения статуса проекта и план восстановления.",
                     "После уведомления куратор продолжает эскалацию по статусу проекта директору.")
    planning = provision("4", "Если план проекта не помещается в срок, куратор эскалирует график руководству.")
    result = answer("У проекта ухудшился статус до красного. Как эскалировать?", planning + list(reversed(rule)))
    assert result.outcome == Outcome.ANSWER and texts(result) == [(a.text, "2") for a in rule]


def test_status_change_does_not_authorize_escalation_for_an_opposite_condition():
    other = provision("4", "Если проект не перешёл в красную зону, куратор эскалирует вопрос в службу планирования.")
    result = answer("Проект покраснел, как эскалировать?", other)
    assert result.outcome == Outcome.NO_BASIS and not result.statements


def test_planning_a_site_visit_requires_a_preparatory_step_rather_than_an_onsite_agenda():
    agenda = provision("4", "На площадке заказчика координатор проводит установочное совещание.",
                       "С представителями заказчика координатор обсуждает график работ.")
    prep = provision("2", "До выезда на объект координатор оформляет пропуска для монтажников.")
    question = "Монтажники выезжают на площадку заказчика, что должен подготовить координатор?"
    assert answer(question, agenda).outcome == Outcome.NO_BASIS
    result = answer(question, agenda + prep)
    assert result.outcome == Outcome.ANSWER and texts(result) == [(prep[0].text, "2")]


def test_condition_on_the_order_does_not_prove_a_status_transition_of_the_project():
    wrong = provision("3", "При переходе заказа в красную зону куратор эскалирует вопрос по проекту.")
    assert answer("У заказчика проект покраснел, как эскалировать?", wrong).outcome == Outcome.NO_BASIS


def test_after_visit_report_does_not_answer_preparation_even_under_a_preparation_heading():
    after = provision("3", "После выезда представитель оформляет отчёт о встрече.", title="Подготовка к выезду")
    assert answer("Инженеры выезжают к клиенту. Что подготовить?", after).outcome == Outcome.NO_BASIS


def test_an_absent_trigger_does_not_activate_positive_instructions():
    status = provision("6", "При переходе проекта в красную зону куратор эскалирует проблему.")
    visit = provision("4", "Перед выездом сотрудник оформляет пропуск.")
    assert answer("Проект не покраснел, как эскалировать?", status).outcome == Outcome.NO_BASIS
    assert answer("Мы не выезжаем на объект, что подготовить?", visit).outcome == Outcome.NO_BASIS
