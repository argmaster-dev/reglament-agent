"""Bound chat work before it reaches PostgreSQL, retrieval or Ollama.

The demo uses one application process and one event loop. Limits are shared across
its sessions. Multiple worker processes would need a shared limiter.
"""

from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
from time import monotonic
from typing import Callable, Iterator

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdmissionSettings(BaseSettings):
    """Demo profile, configurable with CHAT_MAX_IN_FLIGHT and CHAT_*_TURNS_PER_MINUTE."""

    model_config = SettingsConfigDict(env_prefix="CHAT_", extra="ignore")

    max_in_flight: int = Field(default=4, ge=1)
    user_turns_per_minute: int = Field(default=12, ge=1)
    total_turns_per_minute: int = Field(default=36, ge=1)


class AdmissionRejected(RuntimeError):
    """The turn should be reported as busy or rate limited."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TurnAdmission:
    def __init__(self, settings: AdmissionSettings, *, clock: Callable[[], float] = monotonic) -> None:
        self.settings = settings
        self._clock = clock
        self._in_flight: set[str] = set()
        self._history: deque[tuple[float, str]] = deque()
        self._recent: Counter[str] = Counter()

    def _expire(self, now: float) -> None:
        while self._history and self._history[0][0] <= now - 60:
            _, principal = self._history.popleft()
            self._recent[principal] -= 1
            if not self._recent[principal]:
                del self._recent[principal]

    @contextmanager
    def accept(self, principal: str) -> Iterator[None]:
        """Admit a turn immediately or refuse it without creating a waiter."""
        if principal in self._in_flight:
            raise AdmissionRejected("user_busy")
        if len(self._in_flight) >= self.settings.max_in_flight:
            raise AdmissionRejected("stand_busy")

        now = self._clock()
        self._expire(now)
        if self._recent[principal] >= self.settings.user_turns_per_minute:
            raise AdmissionRejected("user_rate")
        if len(self._history) >= self.settings.total_turns_per_minute:
            raise AdmissionRejected("stand_rate")

        self._history.append((now, principal))
        self._recent[principal] += 1
        self._in_flight.add(principal)
        try:
            yield
        finally:
            self._in_flight.remove(principal)
