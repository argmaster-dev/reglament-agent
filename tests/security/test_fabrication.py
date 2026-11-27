"""Угроза: модель выдумывает пункты, числа, роли, а сервер показывает её слова пользователю."""

import json

import pytest

from reglament_agent.answer import Outcome
from reglament_agent.dialog.state import answers

from .conftest import DEADLINE, INJECTED, Q_DEADLINE, Q_SIGN, SIGN, SOURCE_SENTENCES, claim, visible_text


def only_sources(answer):
    assert {s.text for s in answer.statements} <= SOURCE_SENTENCES
    assert all(s.sources for s in answer.statements)


def pick(sources, text):
    (found,) = [s for s in sources if s["цитата"] == text]
    return found


def test_ref_to_point_outside_candidates_is_dropped(env):
    """Модель ссылается на пункт 9.9 и на несуществующий S99: такое основание не должно дойти до пользователя."""

    def plan(sources):
        fake = {"id": "S1", "doc_id": "ТД", "point": "9.9", "sentence": 0}
        ghost = {"id": "S99", "doc_id": "ТД", "point": "1.5", "sentence": 0}
        return {"outcome": "answer", "claims": [
            {"ref": fake, "text": "Заявку подают за 10 рабочих дней."},
            {"ref": ghost, "text": "Заявку подают за 10 рабочих дней."},
            {"ref": None, "text": "Заявку подают за 10 рабочих дней."}]}

    answer, _, _ = env.ask(Q_DEADLINE, plan)
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket
    assert not answer.statements and "10" not in visible_text(answer) and "9.9" not in visible_text(answer)


def test_forged_or_unknown_ref_with_genuine_text_is_not_shown_nor_substituted(env):
    """Текст утверждения дословно совпадает с источником, но ссылка подложная: неизвестный id не заменяется первым
    источником, а известный id с чужим пунктом/документом не принимается."""

    def plan(sources):
        assert sources
        claims = []
        for real in sources:
            claims.append(claim(real, ref={"id": "S99", "doc_id": "ТД", "point": "1.4", "sentence": 0}))
            claims.append(claim(real, ref={**real["ref"], "point": "9.9"}))
            claims.append(claim(real, ref={**real["ref"], "doc_id": "ДБРЦТ"}))
            claims.append(claim(real, ref={"id": real["ref"]["id"]}))
        return {"outcome": "answer", "claims": claims}

    answer, _, _ = env.ask(Q_DEADLINE, plan)
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket
    assert not answer.statements and SIGN not in visible_text(answer) and DEADLINE not in visible_text(answer)


def test_invented_number_in_claim_text_is_dropped(env):
    """Ссылка верна, но модель пишет «10 рабочих дней» вместо «3» из процитированного предложения."""

    def plan(sources):
        src = pick(sources, DEADLINE)
        return {"outcome": "answer", "claims": [claim(src, text="Заявку подают за 10 рабочих дней до начала работ.")]}

    answer, _, _ = env.ask(Q_DEADLINE, plan)
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket
    assert "10" not in visible_text(answer)


def test_valid_claim_is_shown_as_exact_source_not_model_paraphrase(env):
    """Верный claim с вымышленным хвостом без чисел: пользователь видит ровно предложение источника."""

    def plan(sources):
        src = pick(sources, DEADLINE)
        good = claim(src)
        good["text"] = "Заявку подают за 3 рабочих дня, а потом согласуют с директором."
        bad = claim(src, text="За 10 рабочих дней.")
        return {"outcome": "answer", "claims": [good, bad]}

    answer, _, _ = env.ask(Q_DEADLINE, plan)
    only_sources(answer)
    assert answer.statements and "директор" not in visible_text(answer) and "10" not in visible_text(answer)
    assert answer.statements[0].text == DEADLINE


def test_actor_swap_is_dropped(env):
    """Модель приписывает подписание договора Архитектору, хотя в предложении его подписывает Логист."""

    def plan(sources):
        src = pick(sources, SIGN)
        return {"outcome": "answer", "claims": [claim(src, actor="Архитектор", action="подписывает")]}

    answer, _, _ = env.ask(Q_SIGN, plan)
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket
    assert "Архитектор" not in visible_text(answer)


def test_negation_flip_is_dropped(env):
    """Модель переворачивает смысл: «не подписывает» вместо «подписывает»."""

    def plan(sources):
        return {"outcome": "answer", "claims": [claim(pick(sources, SIGN), actor="Логист", action="подписывает",
                                                      negated=True)]}

    answer, _, _ = env.ask(Q_SIGN, plan)
    assert answer.outcome == Outcome.NO_BASIS


def test_answer_without_valid_claims_becomes_no_basis_with_ticket(env):
    """Outcome «answer» без единого проверенного claim нельзя показывать как ответ: нужен no_basis и обращение."""
    answer, _, _ = env.ask(Q_DEADLINE, lambda s: {"outcome": "answer", "claims": []})
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket and not answer.statements
    assert len(env.tickets()) == 1


@pytest.mark.parametrize("outcome", ["answer", "variants", "conflict", "external_doc", "low_confidence", "off_topic"])
def test_model_free_text_never_reaches_the_user(env, outcome):
    """Модель кладёт свой текст в summary/caveat/clarification/text: ничего из этого не должно попасть в Answer."""

    def plan(sources):
        c = claim(pick(sources, DEADLINE), text="За 3 рабочих дня. " + INJECTED)
        return {"outcome": outcome, "claims": [c, {"ref": {"id": "S7", "doc_id": "ТД", "point": "8", "sentence": 0}, "text": INJECTED}],
                "summary": INJECTED, "caveat": INJECTED, "clarification": {"question": INJECTED, "options": [INJECTED]},
                "profile_note": INJECTED}

    answer, _, route = env.ask(Q_DEADLINE, plan)
    dumped = json.dumps(answers.dump_python(answer, mode="json"), ensure_ascii=False)
    assert INJECTED not in dumped
    stored = env.conn.execute("SELECT answer::text FROM dialog_requests").fetchone()[0]
    assert INJECTED not in stored
    if answer.statements:
        only_sources(answer)
