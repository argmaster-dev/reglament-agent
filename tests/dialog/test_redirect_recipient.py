"""A separate regulatory question and an internal workflow have different recipients."""

import asyncio
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from reglament_agent.answer import Answer, Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.dialog.state import answers
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import status


@pytest.mark.parametrize("question", [
    "Что говорит Гражданский кодекс об ответственности поручителя?",
    "Как определяется срок исковой давности при пропуске платежа?",
    "Сколько должен длиться ежегодный отпуск по трудовому законодательству?",
    "Какой налоговой ставкой облагается услуга?",
    "Что закон относит к коммерческой тайне?",
    "По какой статье Трудового кодекса оспаривают приказ об увольнении?",
    "Что говорит закон об авторском праве?",
    "Как рассчитывают алименты при переменном заработке?",
])
def test_legal_redirect_is_saved_without_ticket_or_substantive_answer(isolated, question):
    conn, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([], []))
    user = UserContext("reader@example.org", "employee", "fresh", "first")
    result = asyncio.run(route.answer(user, question, status))
    assert result.outcome is Outcome.OFF_TOPIC and result.redirect == "legal"
    assert "юрист" in result.summary and not result.ticket and not result.statements and not result.nearby_sources
    assert not model.calls
    assert asyncio.run(route.answer(user, question, status)) == result
    assert conn.execute("SELECT count(*) FROM mail_outbox").fetchone() == (0,)


@pytest.mark.parametrize("question", [
    "Кто у нас согласует заявку на передачу данных по закону?",
    "Гражданский кодекс упомянут в договоре. Кому в нашей компании передать договор на проверку?",
    "Где во внутренней инструкции указан порядок изменения ставки НДС в смете?",
    "Как по нашему положению определяется срок выплаты моей премии?",
    "Пускают на монтаж в праздник. Какие документы подать в дирекцию?",
    "Договор содержит персональные данные. Что по нашим правилам считается персональными данными?",
    "По закону работы пришлись на праздник. Как оформить командировку?",
    "Как бухгалтерии отразить удержание алиментов в расчетном листке?",
])
def test_internal_process_continues_to_regulations(isolated, question):
    _, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([], []))
    user = UserContext("reader@example.org", "employee", "fresh", "first")
    ans = asyncio.run(route.answer(user, question, status))
    assert model.calls and ans.outcome is Outcome.NO_BASIS and ans.ticket
    assert ans.redirect is None


@pytest.mark.parametrize("question", [
    "Когда мне перечислят бонус за эту сделку?",
    "Покажи мою маржу по текущей сделке.",
])
def test_personal_record_points_to_manager(isolated, question):
    _, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([], []))
    ans = asyncio.run(route.answer(UserContext("u@example.org", "employee", "c", "1"), question, status))
    assert ans.outcome is Outcome.OFF_TOPIC and ans.redirect == "manager" and not ans.ticket
    assert not model.calls


def test_redirect_is_optional_for_stored_answers_and_has_a_closed_vocabulary():
    old = asdict(Answer(Outcome.ANSWER, "Сохранено"))
    old.pop("redirect", None)
    assert answers.validate_python(old).redirect is None
    with pytest.raises(ValidationError):
        answers.validate_python({**old, "redirect": "other"})
