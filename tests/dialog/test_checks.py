from reglament_agent.dialog.checks import (
    Claim, Evidence, SentenceRef, VerificationResult, decide, verify_claims,
)
from reglament_agent.dialog.lexicon import canonical_role, find_roles, words

S0 = "РП создает Заявку на подрядчика в CRM."
S1 = ("За подготовку, согласование и подписание договора с субподрядчиком отвечает "
      "Логист (Группа по работе с субподрядными организациями).")


def ref(point, n=0, doc="d"):
    return SentenceRef(doc, point, n)


def run(ev_text, column=None, **kw):
    r = ref("x")
    claim = Claim(text=kw.pop("text", "t"), ref=r, **kw)
    res = verify_claims([claim], {r: Evidence(r, ev_text, column)})
    return res.dropped[0].reasons if res.dropped else []


def test_unknown_ref():
    r = ref("1")
    res = verify_claims([Claim("t", r)], {})
    assert res.dropped[0].reasons == ["unknown_ref"] and res.status == "none"


def test_td_16_1_separate_sentences():
    assert run(S1, actor="Логист", action="подписывает") == []
    assert "actor_missing" in run(S1, actor="РП", action="подписывает")
    assert run(S0, actor="РП", action="создает") == []
    assert "action_missing" in run(S0, actor="РП", action="подписывает")


def test_td_16_1_joined():
    joined = S0 + " " + S1
    assert run(joined, actor="РП", action="подписывает") == ["actor_not_bound"]
    assert run(joined, actor="Логист", action="подписание") == []
    assert run(joined, actor="РП", action="создаёт") == []


def test_ib_14_1():
    s = "За заключение договоров с субподрядчиками отвечает РП."
    assert run(s, actor="РП", action="заключает") == []


def test_negation():
    s = "Работы со стороны команды проекта без подписанного договора и согласованной сметы запрещены."
    assert "negation_mismatch" in run(s, action="работы", negated=False)
    assert run(s, action="работы", negated=True) == []
    assert "negation_mismatch" in run(S0, actor="РП", action="создает", negated=True)
    assert run("РП не вправе подписывать договор.", actor="РП", action="подписывает", negated=True) == []
    assert run("РП подписывает договор без согласования.", actor="РП", action="подписывает") == []


def test_numbers():
    s = "Расчёт должен быть инициирован за 2 недели до подачи конкурса."
    assert run(s, text="за 2 недели") == []
    assert "number_missing" in run(s, text="за 3 недели")
    assert run(s, text="за две недели") == []


def test_columns():
    a = "Итоговое технико-коммерческое предложение (ТКП) или конкурсную заявку"
    b = ("Описание системы (паспорт, пояснительную записку или другое архитектурное "
         "описание из проектной документации)")
    assert run(b, column="При наличии", qualifier="обязательно") == ["column_mismatch"]
    assert run(a, column="Обязательно", qualifier="обязательно") == []
    assert run(b, column="При наличии", qualifier="необязательно") == []


def test_pa_numbers():
    s = "У одного ПА может быть максимум 10 заявок с приоритетами от 1 до 4."
    assert run(s, text="не более 10 заявок") == []
    assert "number_missing" in run(s, text="не более 12")


def test_decide():
    c = Claim("t", ref("1"))
    from reglament_agent.dialog.checks import Verdict
    none = VerificationResult([], [Verdict(c, False, ["x"])])
    part = VerificationResult([c], [Verdict(c, False, ["x"])])
    full = VerificationResult([c], [])
    assert decide("answer", none) == ("no_basis", False)
    assert decide("variants", part) == ("variants", True)
    assert decide("answer", full) == ("answer", False)
    assert decide("escalate", none) == ("escalate", False)


def test_lexicon_inflection():
    assert canonical_role("Логистом")[0] == "логист"
    assert find_roles(words("поручено руководителю проекта"))[0][2] == "рп"
    assert canonical_role("РП")[0] == "рп"
    assert find_roles(words("письмо аккаунт-менеджера"))[0][2] == "ам"
    assert find_roles(words("пресейл-архитектор"))[0][2] == "па"
    assert find_roles(words("Менеджера по работе с договорами"))[0][2] == "менеджер по работе с договорами"
