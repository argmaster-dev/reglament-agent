from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CallRecord:
    request_id: str | None
    step: str
    kind: Literal["chat", "embed"]
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    queue_ms: float = 0.0
    total_ms: float = 0.0
    load_ms: float = 0.0
    ok: bool = True
    error: str | None = None
    started_at: str = field(default_factory=_now)


class CallSink(Protocol):
    def record(self, call: CallRecord) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []

    def record(self, call: CallRecord) -> None:
        self.records.append(call)


class JsonlSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, call: CallRecord) -> None:
        line = json.dumps(asdict(call), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
