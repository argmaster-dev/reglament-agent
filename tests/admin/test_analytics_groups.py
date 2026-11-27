"""Semantic grouping of repeated questions: paraphrases join, different mandatory conditions do not."""
import pytest

from reglament_agent.admin import analytics as A
from reglament_agent.ingest.embedding import DIMENSION
from tests.admin.test_analytics import add, client, data, rows  # noqa: F401  (fixtures)
from tests.admin.test_tickets import env  # noqa: F401  (fixture)

PARAPHRASES = ["Сколько дней на согласование договора?", "Сколько дней занимает согласование договора?",
               "Сколько дней длится согласование договора"]


class Controlled:
    """Vectors by text: texts of one family share a direction (cosine ~0.99), families are orthogonal."""
    def __init__(self, families):
        self.families = families  # list of lists of substrings
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = [0.0] * DIMENSION
            for i, fam in enumerate(self.families):
                if any(s in t for s in fam):
                    v[i] = 1.0
                    v[500 + len(t) % 7] += 0.05
                    break
            else:
                v[900] = 1.0
            out.append(v)
        return out


class Broken:
    def embed(self, texts):
        raise ConnectionError("ollama down")


@pytest.fixture
def emb(monkeypatch):
    e = Controlled([["согласование договора", "согласования договора"]])
    from reglament_agent.ingest import embedding
    import contextlib

    @contextlib.contextmanager
    def fake_open(*a, **k):
        yield e
    monkeypatch.setattr(embedding, "open_embedder", fake_open)
    return e


def groups(conn):
    return A.summary(conn, "all")["repeated"]


def test_paraphrases_one_group(data, emb):
    for i, q in enumerate(PARAPHRASES):
        add(data, q, user=f"u{i % 2}@x.local", restated=q)
    add(data, "Совсем другой вопрос про отпуск")
    g = groups(data)
    assert len(g) == 1 and g[0]["count"] == 3 and g[0]["users"] == 2 and g[0]["n_variants"] == 3
    assert A.summary(data, "all")["repeated_exact"] == []  # exact grouping alone would not join them
    page = client("expert").get("/admin/analytics?period=all").text
    assert "формулировки (3)" in page and g[0]["hash"] in page


def pair(conn, a, b):
    add(conn, a, restated=a)
    add(conn, b, restated=b)
    return groups(conn)


@pytest.mark.parametrize("a,b", [
    ("Сколько дней на согласование договора 5 млн?", "Сколько дней на согласование договора 50 млн?"),
    ("Сколько дней на согласование договора?", "Сколько дней на согласование договора без НДС?"),
    ("Сколько дней на согласование договора?", "Сколько дней на согласование договора в юридическом департаменте?"),
    ("Сколько дней на согласование договора?", "Кто отвечает за согласование договора?"),
])
def test_different_conditions_separate(data, emb, a, b):
    emb.families = [["согласование договора", "согласования договора"]]
    assert pair(data, a, b) == []
    # the same pair with cosine ignored would be one group: the guard is what separates them
    sa, sb = A.guard_signature(a), A.guard_signature(b)
    assert sa != sb


def test_same_conditions_join_with_numbers(data, emb):
    g = pair(data, "Сколько дней на согласование договора на 5 млн?", "Сколько дней занимает согласование договора на 5 млн")
    assert len(g) == 1


def test_threshold_respected(monkeypatch):
    e = Controlled([["согласование"], ["отпуск"]])
    a, b = {"id": "1", "question": "согласование договора", "restated": None, "user_email": "a", "created_at": 1,
            "answer": {}, "trace": {}}, {"id": "2", "question": "отпуск сотрудника", "restated": None,
                                         "user_email": "b", "created_at": 2, "answer": {}, "trace": {}}
    assert A.repeated_groups([a, b], e, min_cosine=0.9) == []
    assert len(A.repeated_groups([a, b], e, min_cosine=0.0)) == 1
    monkeypatch.setenv("ANALYTICS_GROUP_MIN_COSINE", "0.5")
    assert A.group_min_cosine() == 0.5
    monkeypatch.setenv("ANALYTICS_GROUP_MIN_COSINE", "junk")
    assert A.group_min_cosine() == 0.90


def test_embedder_failure_falls_back_to_exact(data, monkeypatch):
    from reglament_agent.ingest import embedding
    import contextlib

    @contextlib.contextmanager
    def broken(*a, **k):
        yield Broken()
    monkeypatch.setattr(embedding, "open_embedder", broken)
    add(data, "Сколько дней на согласование договора?")
    add(data, "согласование договора: сколько дней", user="v@x.local")
    add(data, "Как оформить отпуск?")
    r = client("expert").get("/admin/analytics?period=all")
    assert r.status_code == 200 and "Сколько дней на согласование договора?" in r.text
    g = groups(data)
    assert len(g) == 1 and g[0]["count"] == 2


def test_open_embedder_failure_falls_back(data, monkeypatch):
    from reglament_agent.ingest import embedding

    def boom(*a, **k):
        raise RuntimeError("no embedder")
    monkeypatch.setattr(embedding, "open_embedder", boom)
    add(data, "Один вопрос")
    add(data, "один вопрос!")
    assert client("expert").get("/admin/analytics?period=all").status_code == 200
    assert len(groups(data)) == 1


def test_embeddings_cached_per_request(data, emb):
    for q in PARAPHRASES:
        add(data, q, restated=q)
    add(data, PARAPHRASES[0], restated=PARAPHRASES[0])
    groups(data)
    texts = [t for c in emb.calls for t in c]
    assert len(texts) == len(set(texts))


def test_recommendations_use_groups(data, emb):
    for i in range(A.REC_MIN_REPEATS):
        q = PARAPHRASES[i % 3] + " " * i  # whitespace only: distinct raw text
        add(data, q if i < 3 else PARAPHRASES[i % 3], restated=PARAPHRASES[i % 3])
    s = A.summary(data, "all")
    assert s["repeated"][0]["count"] == A.REC_MIN_REPEATS
    assert any("задан 5 раз" in r for r in s["recommendations"])
    assert "задан 5 раз" in client("expert").get("/admin/analytics?period=all").text


def test_outcome_mix_and_csv_group_id(data, emb):
    add(data, PARAPHRASES[0], "answer", restated=PARAPHRASES[0])
    add(data, PARAPHRASES[1], "no_basis", ticket="2026-0001", restated=PARAPHRASES[1])
    add(data, "Другое", restated="Другое")
    g = groups(data)[0]
    assert g["mix"] == {"answered": 1, "no_basis": 1, "ticket": 1}
    page = client("expert").get("/admin/analytics?period=all").text
    assert "ответ 1 · нет в регламентах 1 · тикет 1" in page
    out = rows(client("expert").get("/admin/analytics/requests.csv?period=all").text)
    assert {r["group_id"] for r in out if r["question"] in PARAPHRASES} == {g["hash"]}
    assert [r["group_id"] for r in out if r["question"] == "Другое"] == [""]


def test_html_escaped(data, emb):
    evil = '<script>alert("x")</script> Сколько дней на согласование договора'
    add(data, evil, restated=evil)
    add(data, PARAPHRASES[0] + ' "><img src=x>', restated=PARAPHRASES[0])
    emb.families = [["согласование договора"]]
    page = client("expert").get("/admin/analytics?period=all").text
    assert "<script>alert(" not in page and "&lt;script&gt;" in page
    assert "<img src=x>" not in page
