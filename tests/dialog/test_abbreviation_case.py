import pytest

from reglament_agent.answer import Scope, UserContext
from reglament_agent.dialog.lexicon import expand_query
from reglament_agent.dialog.state import cache_key


@pytest.mark.parametrize("question", ["Кто отвечает по договору?", "По какому регламенту работаем?"])
def test_preposition_is_not_a_software_request(question):
    assert "программное обеспечение" not in expand_query(question).lower()


def test_explicit_software_abbreviation_with_a_preposition():
    assert expand_query("Кто по договору поставляет ПО?").lower().count("программное обеспечение") == 1


def test_reverse_expansion_is_not_blocked_by_a_preposition():
    assert "ПО" in expand_query("По договору предоставляют программное обеспечение").split()


def test_question_cache_preserves_only_meaningful_case(isolated):
    conn, _ = isolated
    user = UserContext("employee@example.org", "employee", "case-check")
    def key(text):
        return cache_key(conn, user, text, Scope(None, None), "qwen")

    assert key("Кто отвечает по договору?") == key("кто отвечает по договору?")
    assert key("Стоимость ПО") != key("Стоимость по")
    assert key("поставка ПО, по договору") != key("поставка по, ПО договору")
