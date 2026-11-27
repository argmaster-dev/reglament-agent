from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary


def provision(point, *sentences):
    return [
        Anchor(SourceRef("dispatch", "Порядок отгрузки", "Склад", 1, point, index),
               text, " ".join(sentences), 0)
        for index, text in enumerate(sentences)
    ]


def select(question, rows, selected):
    candidates = {f"S{i}": row for i, row in enumerate(rows, 1)}
    claims = [{"text": row.text, "ref": model_ref(key, row)}
              for key, row in candidates.items() if row in selected]
    return assemble(question, {"outcome": "answer", "claims": claims}, candidates, ["Склад"], False, {}, rows)


HEADER = "Исполнение проекта начинается при выполнении одного из условий:"


def test_alternative_start_conditions_do_not_replace_verified_prohibition_from_another_point():
    rule = provision("2.3", "При отсутствии согласованной заявки запрещается начинать работы на складе.")
    other = provision("9.4", HEADER, "получена согласованная накладная;", "перевозчик оплатил доставку.")
    answer = select("Можно ли начать работы без согласованной заявки?", other + rule, rule)
    assert answer.outcome == Outcome.ANSWER
    assert [s.text for s in answer.statements] == [rule[0].text]
    assert answer.statements[0].sources == [rule[0].ref]


def test_alternatives_expand_within_selected_point_when_other_points_have_similar_headers():
    shipping = provision("2.3", HEADER, "подписан договор на отгрузку;", "получено разрешение владельца.")
    other = provision("9.4", HEADER, "подписан договор на обучение;", "согласовано обучение.")
    answer = select("Можно ли начать работы без договора?", other + shipping, [shipping[1]])
    assert answer.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in answer.statements] == [(a.text, [a.ref]) for a in shipping]


QUESTION = "Можно стартовать до подписания договора по проекту?"
POST = "После подписания договора диспетчер переводит карточку в статус «Открыт» и начинает этап исполнения проекта."
ORDER = "Менеджер в течение двух дней после подписания договора или согласования старта работ без него оформляет заказ."
HEADER = "Выполнение работ начинается при выполнении одного из условий:"
TERMS = ("подписан договор на ремонт;", "владелец бюджета письменно согласовал риск отказа заказчика от договора.")


def test_quotes_one_applicable_alternative_list_in_source_order_despite_downstream_selection():
    rule = provision("5", HEADER, *TERMS)
    other = provision("1", POST) + provision("2", ORDER)
    rows = other + rule
    answer = select(QUESTION, rows, other)
    assert answer.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in answer.statements] == [(a.text, [a.ref]) for a in rule]
    assert not any(a.ref.point in {"1", "2"} for a in vocabulary(QUESTION, rows).values())


def test_after_signature_action_and_foreign_conditions_do_not_establish_prior_permission():
    other = provision("1", POST) + provision("2", ORDER)
    delivery = provision("3", "Проверка исполнения проекта начинается при выполнении одного из условий:", *TERMS)
    different_document = provision("4", HEADER, "подписан акт;", "начальник смены дал согласие.")
    rows = other + delivery + different_document
    answer = select(QUESTION, rows, other)
    assert answer.outcome == Outcome.NO_BASIS and not answer.statements


def test_two_possible_alternative_lists_require_clarification():
    r1 = provision("3", HEADER, *TERMS)
    r2 = provision("4", HEADER, "подписан договор на доставку;", "получено разрешение директора.")
    other = provision("1", POST)
    answer = select(QUESTION, other + r1 + r2, other)
    assert answer.outcome == Outcome.NO_BASIS and not answer.statements


def test_direct_before_signature_prohibition_applies_to_work_only():
    rule = provision("3", "До подписания договора начинать работы запрещено.")
    crm = provision("2", "До подписания договора разрешено начать оформление карточки проекта.")
    answer = select(QUESTION, crm + rule, crm + rule)
    assert answer.outcome == Outcome.ANSWER
    assert [s.sources for s in answer.statements] == [[rule[0].ref]]


def test_only_after_signature_can_prohibit_early_start():
    rule = provision("3", "Начинать работы разрешается только после подписания договора.")
    answer = select(QUESTION, rule, rule)
    assert answer.outcome == Outcome.ANSWER and [s.sources for s in answer.statements] == [[rule[0].ref]]


def test_precedes_a_different_signing_event():
    rule = provision("3", "Разрешено начать работы после подписания договора и до подписания акта.")
    answer = select(QUESTION, rule, rule)
    assert answer.outcome == Outcome.NO_BASIS and not answer.statements
