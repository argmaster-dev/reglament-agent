"""Similar-phrasing cache (dialog-p01): off by default, reuses an answer only if nothing that matters differs."""

import pytest

from reglament_agent.answer import Scope, UserContext
from reglament_agent.dialog import similar, state
from reglament_agent.dialog.route import Dialog
from tests.dialog.test_cache import Model, ask, publish, route_for, user, world

BASE = "Кто подписывает договор в ТД?"


@pytest.mark.parametrize("old_policy", ["extract-v7-document-subject", "extract-v8-scoped-evidence"])
def test_new_evidence_rules_invalidate_exact_and_similar_answers(isolated, monkeypatch, old_policy):
    conn, _ = isolated
    employee = UserContext("reader@example.org", "employee", "chat", "request")
    scope = Scope(None, None)

    def keys():
        return state.cache_key(conn, employee, BASE, scope, "model"), state.cache_context(conn, employee, scope, "model")

    exact, context = keys()
    monkeypatch.setattr(state, "POLICY", old_policy)
    previous_exact, previous_context = keys()
    assert exact != previous_exact
    assert context != previous_context


class Embedder(Model):
    """Fake embedder: every question gets the same vector unless a test names another one."""

    def __init__(self, vectors=None):
        super().__init__()
        self.vectors = vectors or {}
        self.embedded = []

    async def embed(self, texts, **kw):
        self.embedded += texts
        return [self.vectors.get(t, [0.01] * 1024) for t in texts]


def route(conn, dsn, model, enabled=True, **kw):
    return route_for(conn, dsn, model, similar_cache=enabled, **kw)


def trace(conn, answer_id):
    return conn.execute("SELECT trace FROM dialog_requests WHERE id=%s", (answer_id,)).fetchone()[0]


def test_off_by_default(isolated, monkeypatch):
    monkeypatch.delenv("CACHE_SIMILAR_ENABLED", raising=False)
    assert similar.settings({}) == (False, 0.97)
    assert similar.settings({"CACHE_SIMILAR_ENABLED": "true", "CACHE_SIMILAR_MIN_COSINE": "0.99"}) == (True, 0.99)
    assert similar.settings({"CACHE_SIMILAR_MIN_COSINE": "junk"}) == (False, 0.97)
    conn, dsn = isolated
    assert Dialog(dsn, Embedder()).similar_cache is False
    monkeypatch.setenv("CACHE_SIMILAR_ENABLED", "1")
    assert Dialog(dsn, Embedder()).similar_cache is True


def test_disabled_never_hits_even_for_a_paraphrase(isolated):
    conn, dsn = isolated
    world(conn)
    model = Embedder()
    r = route(conn, dsn, model, enabled=False)
    ask(r, user("a@example.org", "a"), BASE)
    second = ask(r, user("b@example.org", "b"), "Подскажите, кто подписывает договор в ТД?")
    assert not second.from_cache and len(model.calls) == 2
    # Entries made while disabled already carry the embedding: enabling later finds them.
    assert conn.execute("SELECT count(*) FROM dialog_requests WHERE cache_embedding IS NOT NULL").fetchone()[0] == 2
    on = route(conn, dsn, model, enabled=True)
    third = ask(on, user("c@example.org", "c"), "Подскажите, а кто подписывает договор в ТД?")
    assert third.from_cache and len(model.calls) == 2


def test_paraphrase_with_the_same_meaning_hits_when_enabled(isolated):
    conn, dsn = isolated
    world(conn)
    model = Embedder()
    r = route(conn, dsn, model)
    first = ask(r, user("alice@example.org", "a"), BASE)
    second = ask(r, user("bob@example.org", "b"), "Подскажите, кто подписывает договор в ТД?")
    assert second.from_cache and len(model.calls) == 1
    assert second.statements == first.statements and second.ticket is None and second.profile_note is None
    t = trace(conn, second.answer_id)
    assert "cache_hit" in t["steps"] and "cache_similar_hit" in t["steps"]
    assert t["cache"]["kind"] == "similar" and t["cache"]["source"] == first.answer_id and t["cache"]["cosine"] >= 0.97
    assert "alice" not in str(t)
    assert conn.execute("SELECT cache_key FROM dialog_requests WHERE id=%s", (second.answer_id,)).fetchone() == (None,)


@pytest.mark.parametrize("first,second", [
    ("Кто подписывает договор в ТД в течение 5 дней?", "Кто подписывает договор в ТД в течение 7 дней?"),
    ("Кто подписывает договор в ТД?", "Кто подписывает договор в ТД без согласования?"),
    ("Кто подписывает договор в ТД?", "Кто не подписывает договор в ТД?"),
    ("Кто подписывает договор в ТД?", "Кто подписывает договор в ИБ?"),
    ("Кто подписывает договор в ТД?", "Когда подписывается договор в ТД?"),
    ("Кто подписывает договор в ТД?", "Сколько подписывают договор в ТД?"),
    ("Кто подписывает договор на этапе пресейла в ТД?", "Кто подписывает договор в ТД?"),
])
def test_changed_condition_is_a_miss_even_with_identical_embeddings(isolated, first, second):
    conn, dsn = isolated
    world(conn)
    model = Embedder()  # every vector equal: only the condition checks can refuse
    r = route(conn, dsn, model)
    ask(r, user("a@example.org", "a"), first)
    answer = ask(r, user("b@example.org", "b"), second)
    assert not answer.from_cache
    assert "cache_similar_hit" not in trace(conn, answer.answer_id)["steps"]


def test_low_cosine_is_a_miss(isolated):
    conn, dsn = isolated
    world(conn)
    other = "Подскажите, кто подписывает договор в ТД?"
    model = Embedder({other: [1.0] + [0.0] * 1023})
    r = route(conn, dsn, model, similar_min_cosine=0.97)
    ask(r, user("a@example.org", "a"), BASE)
    assert not ask(r, user("b@example.org", "b"), other).from_cache
    # The same pair passes when the threshold is lowered below its cosine.
    cos = similar.cosine([0.01] * 1024, [1.0] + [0.0] * 1023)
    low = route(conn, dsn, model, similar_min_cosine=cos - 0.001)
    assert ask(low, user("c@example.org", "c"), other).from_cache


def test_new_publication_or_scope_breaks_the_similar_hit(isolated):
    conn, dsn = isolated
    world(conn)
    model = Embedder()
    r = route(conn, dsn, model)
    paraphrase = "Подскажите, кто подписывает договор в ТД?"
    ask(r, user("a@example.org", "a"), BASE)
    assert ask(r, user("b@example.org", "b"), paraphrase).from_cache
    publish(conn, "td", ["Договор подписывает юрист: договор."])
    fresh = ask(r, user("c@example.org", "c"), "Подскажите, а кто подписывает договор в ТД?")
    assert not fresh.from_cache and "юрист" in fresh.statements[0].text


def test_signature_checks():
    d = ["ТД", "ИБ"]
    assert similar.same_condition(BASE, "Подскажите, кто подписывает договор в ТД?", d)
    assert not similar.same_condition("срок 5 дней", "срок 7 дней", d)
    assert not similar.same_condition("срок 5 дней", "срок 5.5 дней", d)
    assert not similar.same_condition("можно ли отпуск", "нельзя ли отпуск", d)
    assert not similar.same_condition("где подписать", "когда подписать", d)
    assert not similar.same_condition("кто подписывает в ТД", "кто подписывает в ИБ", d)
    assert similar.cosine([1, 0], [1, 0]) == 1 and similar.cosine([1, 0], [0, 1]) == 0 and similar.cosine([], [1]) == 0
