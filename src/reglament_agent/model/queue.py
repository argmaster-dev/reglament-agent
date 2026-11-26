from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from .errors import QueueFull

PositionCallback = Callable[[int], None]

log = logging.getLogger(__name__)


@dataclass
class Ticket:
    queue_ms: float = 0.0


class _Waiter:
    def __init__(self, on_position: PositionCallback | None) -> None:
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.on_position = on_position
        self.last_position = 0


class GenerationSlot:
    """Single (or N) generation slot; FIFO. Position 1 = next in line."""

    def __init__(self, capacity: int = 1, max_queue: int = 0) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if max_queue < 0:
            raise ValueError("max_queue must be >= 0")
        self.capacity = capacity
        self.max_queue = max_queue  # 0 = unlimited
        self.active = 0
        self._queue: deque[_Waiter] = deque()

    @property
    def waiting(self) -> int:
        return len(self._queue)

    def slot_status(self) -> dict:
        return {"slots": self.capacity, "busy": self.active, "waiting": len(self._queue)}

    def _notify(self) -> None:
        for i, w in enumerate(self._queue, start=1):
            if w.on_position and w.last_position != i:
                w.last_position = i
                try:
                    w.on_position(i)
                except Exception:  # a UI callback must never stall the generation queue
                    log.exception("on_position callback failed")

    def _grant(self) -> None:
        while self._queue and self.active < self.capacity:
            w = self._queue.popleft()
            if w.future.done():  # cancelled
                continue
            self.active += 1
            w.future.set_result(None)
        self._notify()

    @asynccontextmanager
    async def acquire(self, on_position: PositionCallback | None = None) -> AsyncIterator[Ticket]:
        start = time.perf_counter()
        if self.active < self.capacity and not self._queue:
            self.active += 1
        else:
            if self.max_queue and len(self._queue) >= self.max_queue:
                raise QueueFull(f"generation queue is full ({self.max_queue})")
            w = _Waiter(on_position)
            self._queue.append(w)
            self._notify()
            try:
                await w.future
            except BaseException:
                if w in self._queue:
                    self._queue.remove(w)
                    self._notify()
                elif w.future.done() and not w.future.cancelled():
                    # granted but cancelled before resuming: hand the slot on
                    self.active -= 1
                    self._grant()
                raise
        ticket = Ticket(queue_ms=(time.perf_counter() - start) * 1000)
        try:
            yield ticket
        finally:
            self.active -= 1
            self._grant()


@asynccontextmanager
async def generation_progress(send):
    """Deliver slot events in order, in the requesting session's async context."""
    positions = asyncio.Queue()

    async def deliver():
        while (position := await positions.get()) is not None:
            try:
                await send(f"В очереди: {position}" if position else "Формирую ответ…")
            except Exception:
                log.exception("Could not display generation progress")

    task = asyncio.create_task(deliver())
    try:
        yield positions.put_nowait
    finally:
        positions.put_nowait(None)
        await task
