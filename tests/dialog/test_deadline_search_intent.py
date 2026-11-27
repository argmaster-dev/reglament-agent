import pytest

from reglament_agent.dialog.deadlines import question_mode, ranking_pattern


@pytest.mark.parametrize("query", [
    "заявка пресейл срок конкурса",
    "хранение архив сроки ведомость",
])
def test_deadline_keyword_without_request_keeps_neutral_search_ranking(query):
    assert question_mode(query) is None
    assert ranking_pattern(query) == ""


@pytest.mark.parametrize(("question", "mode"), [
    ("Срок подачи заявления на отпуск", "any"),
    ("В какой срок сотрудник сдаёт ведомость?", "any"),
    ("Каков предельный срок оформления пропуска?", "any"),
    ("Уточните срок подачи предложения.", "any"),
    ("Подскажите, сколько дней даётся на согласование заявки?", "any"),
    ("Когда крайний срок запуска расчёта перед конкурсом?", "any"),
    ("За сколько дней до начала работ подаётся заявка?", "before"),
    ("Сколько дней после получения нужно проверить заявку?", "after"),
])
def test_explicit_deadline_requests_keep_time_aware_ranking(question, mode):
    assert question_mode(question) == mode
    assert ranking_pattern(question)
