import asyncio
from contextlib import contextmanager
from time import monotonic
from types import SimpleNamespace

from reglament_agent.admin import analytics
from reglament_agent.ingest import embedding


def _rows():
    questions = ["Как согласовать договор?", "Как согласовать договор?",
                 "Как открыть счёт?", "Как оформить заявку?"]
    return [dict(id=str(i), user_email="user", question=q, restated=q,
                 created_at=i, answer={"outcome": "answer"}, trace={})
            for i, q in enumerate(questions)]


def test_slow_batches_cancel_and_page_keeps_exact_groups(monkeypatch):
    events = []

    class SlowClient:
        def __init__(self, settings):
            pass

        async def embed(self, texts, **kwargs):
            try:
                for _ in texts:
                    await asyncio.sleep(0.08)
                    events.append("batch")
                return [[1., 0.] for _ in texts]
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

        async def aclose(self):
            events.append("closed")

    monkeypatch.setenv("EMBEDDER", "ollama")
    monkeypatch.setattr(embedding, "OllamaClient", SlowClient)
    monkeypatch.setattr(analytics, "GROUP_SEMANTIC_BUDGET_S", 0.12, raising=False)
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: []))
    started = monotonic()
    groups = analytics.build_groups(conn, _rows())
    assert monotonic() - started < 1.0
    assert events[-2:] == ["cancelled", "closed"]
    assert [set(g["row_ids"]) for g in groups] == [{"0", "1"}]


def test_embeddings_arriving_after_the_budget_do_not_merge_groups(monkeypatch):
    now = [0.0]

    @contextmanager
    def late_result(**kwargs):
        def embed(texts):
            now[0] += 3.0
            return [[1., 0.] for _ in texts]
        yield SimpleNamespace(embed=embed)

    monkeypatch.setattr(analytics, "monotonic", lambda: now[0], raising=False)
    monkeypatch.setattr(embedding, "open_embedder", late_result)
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: []))
    assert [set(g["row_ids"]) for g in analytics.build_groups(conn, _rows())] == [{"0", "1"}]
