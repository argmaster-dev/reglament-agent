import asyncio

import httpx
import pytest
from pydantic import ValidationError

from reglament_agent.model import (
    GenerationSlot, ModelSettings, OllamaClient,
)
from reglament_agent.model.queue import GenerationQueueFull

from reglament_agent.model import MemorySink, ModelError  # noqa: E402
from reglament_agent.ops.health import Settings, health as health_check  # noqa: E402

from .test_model import SCHEMA, chat_body


def _core(st):
    return {k: st[k] for k in ("slots", "busy", "waiting")}


class Holder:
    """Acquires a slot and holds it until released; records positions."""

    def __init__(self, slot):
        self.slot = slot
        self.positions = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    async def run(self):
        async with self.slot.acquire(on_position=self.positions.append):
            self.started.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("boom")


async def spawn(slot, n):
    hs = [Holder(slot) for _ in range(n)]
    tasks = []
    for h in hs:
        tasks.append(asyncio.create_task(h.run()))
        await asyncio.sleep(0)
    return hs, tasks


async def test_three_run_concurrently_fourth_waits():
    slot = GenerationSlot(3)
    hs, tasks = await spawn(slot, 4)
    assert [h.started.is_set() for h in hs] == [True, True, True, False]
    assert hs[3].positions == [1]
    assert _core(slot.slot_status()) == {"slots": 3, "busy": 3, "waiting": 1}
    hs[0].release.set()
    await asyncio.wait_for(hs[3].started.wait(), 1)
    assert _core(slot.slot_status()) == {"slots": 3, "busy": 3, "waiting": 0}
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    assert _core(slot.slot_status()) == {"slots": 3, "busy": 0, "waiting": 0}


async def test_fifo_positions_with_slots():
    slot = GenerationSlot(2)
    hs, tasks = await spawn(slot, 5)
    assert [h.positions for h in hs[2:]] == [[1], [2], [3]]
    hs[0].release.set()
    await asyncio.wait_for(hs[2].started.wait(), 1)
    assert not hs[3].started.is_set()
    assert hs[3].positions == [2, 1] and hs[4].positions == [3, 2]
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)


async def test_cancelled_waiter_removed_and_positions_updated():
    slot = GenerationSlot(1)
    hs, tasks = await spawn(slot, 4)
    assert slot.waiting == 3
    tasks[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[1]
    assert slot.waiting == 2
    assert hs[2].positions == [2, 1] and hs[3].positions == [3, 2]
    for h in hs:
        h.release.set()
    await asyncio.gather(tasks[0], tasks[2], tasks[3])
    assert hs[1].started.is_set() is False
    assert _core(slot.slot_status()) == {"slots": 1, "busy": 0, "waiting": 0}


async def test_exception_releases_slot():
    slot = GenerationSlot(2)
    hs, tasks = await spawn(slot, 3)
    hs[0].fail = True
    hs[0].release.set()
    with pytest.raises(RuntimeError):
        await tasks[0]
    await asyncio.wait_for(hs[2].started.wait(), 1)
    hs[1].release.set()
    hs[2].release.set()
    await asyncio.gather(tasks[1], tasks[2])
    assert slot.slot_status()["busy"] == 0


async def test_queue_limit_raises_generation_queue_full():
    slot = GenerationSlot(1, max_waiting=1)
    hs, tasks = await spawn(slot, 2)  # 1 active + 1 waiting (full)
    with pytest.raises(GenerationQueueFull):
        async with slot.acquire():
            pass
    assert slot.waiting == 1
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    async with slot.acquire():  # free again
        pass


async def test_queue_limit_with_three_slots():
    slot = GenerationSlot(3, max_waiting=2)
    hs, tasks = await spawn(slot, 5)  # 3 active + 2 waiting
    assert _core(slot.slot_status()) == {"slots": 3, "busy": 3, "waiting": 2}
    with pytest.raises(GenerationQueueFull):
        async with slot.acquire():
            pass
    assert slot.waiting == 2 and slot.active == 3
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)


async def test_concurrency_never_exceeds_n():
    for n in (1, 3):
        slot = GenerationSlot(n, max_waiting=50)
        running = peak = 0

        async def job():
            nonlocal running, peak
            async with slot.acquire():
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*(job() for _ in range(12)))
        assert peak == n


async def test_slot_recovered_after_exception_in_chat_call():
    calls = 0

    async def h(req):
        nonlocal calls
        calls += 1
        return httpx.Response(500 if calls <= 3 else 200, json=chat_body())

    c = OllamaClient(ModelSettings(chat_slots=3), transport=httpx.MockTransport(h))
    for i in range(3):
        with pytest.raises(ModelError):
            await c.chat_json([], SCHEMA, step=f"f{i}")
    assert _core(c.slot_status()) == {"slots": 3, "busy": 0, "waiting": 0}
    results = await asyncio.gather(*(c.chat_json([], SCHEMA, step=f"ok{i}") for i in range(3)))
    assert len(results) == 3


async def test_single_slot_unchanged():
    slot = GenerationSlot(1)
    hs, tasks = await spawn(slot, 3)
    assert _core(slot.slot_status()) == {"slots": 1, "busy": 1, "waiting": 2}
    assert [h.positions for h in hs[1:]] == [[1], [2]]
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    assert [h.positions for h in hs[1:]] == [[1], [2, 1]]


def test_settings_defaults_and_validation(monkeypatch):
    s = ModelSettings()
    assert s.chat_slots == 1 and s.generation_max_waiters == 3
    monkeypatch.setenv("MODEL_CHAT_SLOTS", "4")
    assert ModelSettings().chat_slots == 4
    for bad in ("0", "-1"):
        monkeypatch.setenv("MODEL_CHAT_SLOTS", bad)
        with pytest.raises(ValidationError):
            ModelSettings()


def test_default_queue_limit_scales_with_slots():
    one = OllamaClient(ModelSettings(chat_slots=1, generation_max_waiters=3))
    three = OllamaClient(ModelSettings(chat_slots=3, generation_max_waiters=3))
    assert (one.slot.capacity, one.slot.max_waiting) == (1, 3)  # same as before slots existed
    assert (three.slot.capacity, three.slot.max_waiting) == (3, 9)


async def test_client_n3_queue_position_overflow_and_journal():
    gate = asyncio.Event()

    async def h(req):
        await gate.wait()
        return httpx.Response(200, json=chat_body())

    sink = MemorySink()
    c = OllamaClient(ModelSettings(chat_slots=3, generation_max_waiters=1), sink=sink,
                     transport=httpx.MockTransport(h))
    assert _core(c.slot_status()) == {"slots": 3, "busy": 0, "waiting": 0}
    positions = []
    ts = [asyncio.create_task(c.chat_json([], SCHEMA, step=str(i))) for i in range(3)]
    while c.slot_status()["busy"] < 3:
        await asyncio.sleep(0)
    ts += [asyncio.create_task(c.chat_json([], SCHEMA, step=f"w{i}", on_position=positions.append))
           for i in range(3)]  # limit = 1 * 3 waiters
    while c.slot_status()["waiting"] < 3:
        await asyncio.sleep(0)
    assert _core(c.slot_status()) == {"slots": 3, "busy": 3, "waiting": 3}
    with pytest.raises(GenerationQueueFull):
        await c.chat_json([], SCHEMA, step="x")
    gate.set()
    await asyncio.gather(*ts)
    assert _core(c.slot_status()) == {"slots": 3, "busy": 0, "waiting": 0}
    done = {r.step: r for r in sink.records if r.ok}
    assert all(r.slots == 3 for r in done.values())
    assert done["w2"].waiting == 2 and done["0"].waiting == 0
    assert max(r.busy for r in done.values()) == 3
    assert not [r for r in sink.records if r.step == "x" and r.busy]  # rejected call never got a slot


async def test_health_reports_slots_busy_waiting(monkeypatch):
    from reglament_agent.model import queue as queue_mod

    monkeypatch.setattr(queue_mod, "_chat_slot", None)
    gate = asyncio.Event()

    async def h(req):
        await gate.wait()
        return httpx.Response(200, json=chat_body())

    c = OllamaClient(ModelSettings(chat_slots=2), transport=httpx.MockTransport(h))
    queue_mod.register_chat_slot(c.slot)
    ts = [asyncio.create_task(c.chat_json([], SCHEMA, step=str(i))) for i in range(3)]
    while c.slot_status()["waiting"] < 1:
        await asyncio.sleep(0)

    def refuse(request):
        raise httpx.ConnectError("down")

    body = await health_check(Settings(database_url="", ollama_url="http://o.test", mail_port=1,
                                       timeout=1, transport=httpx.MockTransport(refuse)))
    g = body["generation"]
    assert (g["slots"], g["busy"], g["waiting"]) == (2, 2, 1)
    gate.set()
    await asyncio.gather(*ts)
    body = await health_check(Settings(database_url="", ollama_url="http://o.test", mail_port=1,
                                       timeout=1, transport=httpx.MockTransport(refuse)))
    assert (body["generation"]["busy"], body["generation"]["waiting"]) == (0, 0)


async def test_health_ignores_indexer_embedding_client(monkeypatch):
    """The standard indexer client (open_embedder) has its own slot; /health must not add it up."""
    from reglament_agent.ingest.embedding import open_embedder
    from reglament_agent.model import queue as queue_mod

    monkeypatch.setattr(queue_mod, "_chat_slot", None)
    monkeypatch.setenv("EMBEDDER", "ollama")
    chat = OllamaClient(ModelSettings(chat_slots=3, generation_max_waiters=1))
    queue_mod.register_chat_slot(chat.slot)

    def refuse(request):
        raise httpx.ConnectError("down")

    def read():
        cfg = Settings(database_url="", ollama_url="http://o.test", mail_port=1, timeout=1,
                       transport=httpx.MockTransport(refuse))
        return asyncio.run(health_check(cfg))["generation"]

    expected = chat.slot_status()
    assert (expected["slots"], expected["max_waiting"]) == (3, 3)
    before = await asyncio.to_thread(read)
    def with_indexer():
        with open_embedder():  # real function, creates its own OllamaClient + GenerationSlot
            return read()
    during = await asyncio.to_thread(with_indexer)
    after = await asyncio.to_thread(read)
    assert before == during == after == expected
