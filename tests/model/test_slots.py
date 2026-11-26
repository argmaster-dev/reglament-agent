import asyncio

import httpx
import pytest
from pydantic import ValidationError

from reglament_agent.model import (
    GenerationSlot, ModelError, ModelSettings, OllamaClient, QueueFull,
)

from .test_model import SCHEMA, chat_body


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
    assert slot.slot_status() == {"slots": 3, "busy": 3, "waiting": 1}
    hs[0].release.set()
    await asyncio.wait_for(hs[3].started.wait(), 1)
    assert slot.slot_status() == {"slots": 3, "busy": 3, "waiting": 0}
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    assert slot.slot_status() == {"slots": 3, "busy": 0, "waiting": 0}


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
    assert slot.slot_status() == {"slots": 1, "busy": 0, "waiting": 0}


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


async def test_queue_max_raises_queue_full():
    slot = GenerationSlot(1, max_queue=1)
    hs, tasks = await spawn(slot, 2)  # 1 active + 1 waiting (full)
    with pytest.raises(QueueFull):
        async with slot.acquire():
            pass
    assert slot.waiting == 1
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    async with slot.acquire():  # free again
        pass


def test_queue_full_is_model_error():
    assert issubclass(QueueFull, ModelError)


async def test_single_slot_unchanged():
    slot = GenerationSlot(1)
    hs, tasks = await spawn(slot, 3)
    assert slot.slot_status() == {"slots": 1, "busy": 1, "waiting": 2}
    assert [h.positions for h in hs[1:]] == [[1], [2]]
    for h in hs:
        h.release.set()
    await asyncio.gather(*tasks)
    assert [h.positions for h in hs[1:]] == [[1], [2, 1]]


def test_settings_defaults_and_validation(monkeypatch):
    s = ModelSettings()
    assert s.chat_slots == 1 and s.chat_queue_max == 0
    monkeypatch.setenv("MODEL_CHAT_SLOTS", "4")
    monkeypatch.setenv("MODEL_CHAT_QUEUE_MAX", "8")
    s = ModelSettings()
    assert s.chat_slots == 4 and s.chat_queue_max == 8
    for bad in ("0", "-1"):
        monkeypatch.setenv("MODEL_CHAT_SLOTS", bad)
        with pytest.raises(ValidationError):
            ModelSettings()
    monkeypatch.setenv("MODEL_CHAT_SLOTS", "1")
    monkeypatch.setenv("MODEL_CHAT_QUEUE_MAX", "-1")
    with pytest.raises(ValidationError):
        ModelSettings()


async def test_client_uses_settings_and_exposes_status():
    gate = asyncio.Event()

    async def h(req):
        await gate.wait()
        return httpx.Response(200, json=chat_body())

    c = OllamaClient(ModelSettings(chat_slots=2, chat_queue_max=1),
                     transport=httpx.MockTransport(h))
    assert c.slot_status() == {"slots": 2, "busy": 0, "waiting": 0}
    ts = [asyncio.create_task(c.chat_json([], SCHEMA, step=str(i))) for i in range(3)]
    while c.slot_status()["waiting"] < 1:
        await asyncio.sleep(0)
    assert c.slot_status() == {"slots": 2, "busy": 2, "waiting": 1}
    with pytest.raises(QueueFull):
        await c.chat_json([], SCHEMA, step="x")
    gate.set()
    await asyncio.gather(*ts)
