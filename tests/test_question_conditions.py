from types import SimpleNamespace

import pytest

from reglament_agent.admin import analytics
from reglament_agent.dialog import similar


def log_rows(*texts):
    return [dict(id=str(i), created_at=i, user_email=str(i), question=t, restated=t,
                 answer={"outcome": "answer"}, trace={}) for i, t in enumerate(texts)]


# Even if the embedding model cannot distinguish any pair in this test.
SAME_VECTOR = SimpleNamespace(embed=lambda texts: [[1., 0.] for _ in texts])
DIFFERENT_CONDITIONS = [
    ("Кто согласует заявку до 5 дней?", "Кто согласует заявку от 5 дней?"),
    ("Кто согласует заявку до пяти дней?", "Кто согласует заявку после пяти дней?"),
    ("Кто согласует заявку до двух рабочих часов?", "Кто согласует заявку от двух рабочих часов?"),
    ("Кто согласует заявку раньше заседания?", "Кто согласует заявку позже заседания?"),
    ("Кто утверждает сумму не выше 5 млн?", "Кто утверждает сумму не ниже 5 млн?"),
    ("Каков минимальный срок?", "Каков максимальный срок?"),
    ("Кто согласует до подписания?", "Кто согласует до оплаты?"),
    ("Где передать отчёт?", "Куда передать отчёт?"),
    ("Кто согласует заявку за 5 дней?", "Кто согласует заявку за 5 недель?"),
    ("Кто согласует заявку за 5 рабочих дней?", "Кто согласует заявку за 5 календарных дней?"),
    ("Кто согласует сумму < 500 рублей?", "Кто согласует сумму > 500 рублей?"),
    ("Кто согласует сумму от 3 до 5 тысяч?", "Кто согласует сумму от 5 до 3 тысяч?"),
    ("Кто согласует заявку за пять дней?", "Кто согласует заявку за семь дней?"),
    ("Когда согласовать договор?", "Где согласовать договор?"),
    ("Кто согласует договор в ТД?", "Кто не согласует договор в ТД?"),
    ("Кто согласует договор в ТД?", "Кто согласует договор в ИБ?"),
]


@pytest.mark.parametrize("first,second", DIFFERENT_CONDITIONS)
def test_compulsory_conditions_in_similar_cache(first, second):
    candidate = ({"outcome": "answer"}, "old-answer", first, [1., 0.])
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [candidate]))
    assert similar.find(conn, "context", second, [1., 0.001], ["ТД", "ИБ"], 0.97) is None


@pytest.mark.parametrize("emb", [None, SAME_VECTOR], ids=["exact", "semantic"])
@pytest.mark.parametrize("first,second", DIFFERENT_CONDITIONS)
def test_analytics_groups_keep_compulsory_conditions(first, second, emb):
    rows = log_rows(first, first, second, second)
    groups = analytics.repeated_groups(rows, emb, dept_vocab=frozenset({"тд", "иб"}))
    assert {frozenset(g["row_ids"]) for g in groups} == {frozenset(["0", "1"]), frozenset(["2", "3"])}
    assert len({g["hash"] for g in groups}) == 2


def test_unambiguous_paraphrase_preserves_the_same_bound():
    first = "Кто согласует заявку на срок до 5 рабочих дней?"
    second = "Подскажите, кто согласует заявку на срок до 5 рабочих дней?"
    assert similar.same_condition(first, second, [])
    assert len(analytics.repeated_groups(log_rows(first, second), SAME_VECTOR)) == 1


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1", "1.01"])
def test_invalid_cosine_setting_uses_safe_default(bad):
    assert similar.settings({"CACHE_SIMILAR_MIN_COSINE": bad}) == (False, similar.DEFAULT_MIN_COSINE)


def test_cache_find_rejects_an_invalid_threshold_before_reading_candidates():
    assert similar.find(None, "context", "Как согласовать?", [1.], [], float("nan")) is None


def test_exact_analytics_summary_keeps_unlike_questions_apart():
    first, second = "Когда согласовать договор?", "Где согласовать договор?"
    assert analytics.repeated(log_rows(first, second)) == []


@pytest.mark.parametrize("emb", [None, SAME_VECTOR])
def test_restatement_cannot_erase_a_bound_from_the_original(emb):
    rows = log_rows("Когда согласовать отчёт до 5 дней?", "Когда согласовать отчёт от 5 дней?")
    for row in rows:
        row["restated"] = "Когда согласовать отчёт за 5 дней?"
    assert analytics.repeated_groups(rows, emb) == []
