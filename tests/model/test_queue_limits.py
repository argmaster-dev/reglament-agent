import asyncio

import pytest

from reglament_agent.model.queue import GenerationQueueFull, GenerationSlot


async def test_queue_refuses_overflow_keeps_fifo_and_recovers_after_cancellation():
    slot = GenerationSlot(1, max_waiting=2)
    positions = []
    completed = []

    async def waiter(name):
        async with slot.acquire(lambda position: positions.append((name, position))):
            completed.append(name)

    async with slot.acquire():
        first = asyncio.create_task(waiter("first"))
        next_in_line = asyncio.create_task(waiter("next"))
        await asyncio.sleep(0)
        assert slot.waiting == 2
        with pytest.raises(GenerationQueueFull):
            async with slot.acquire():
                pytest.fail("Queue overflow reached the model")
        assert slot.waiting == 2 and slot.active == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        last = asyncio.create_task(waiter("last"))
        await asyncio.sleep(0)
        assert slot.waiting == 2

    await asyncio.gather(next_in_line, last)
    assert completed == ["next", "last"]
    assert ("next", 1) in positions
    assert slot.waiting == 0 and slot.active == 0


async def test_zero_waiting_slots_uses_the_free_slot_and_never_enqueues():
    slot = GenerationSlot(1, max_waiting=0)
    async with slot.acquire():
        with pytest.raises(GenerationQueueFull):
            async with slot.acquire():
                pytest.fail("Queued while queuing is disabled")
        assert slot.waiting == 0
    async with slot.acquire():
        assert slot.active == 1
