from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from .calls import CallRecord, CallSink, MemorySink
from .queue import GenerationSlot
from .settings import ModelSettings

EMBED_DIM = 1024


class ModelError(Exception):
    pass


class ModelUnavailable(ModelError):
    pass


class ModelOutputError(ModelError):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class ChatResult:
    data: dict
    raw: str
    prompt_tokens: int
    completion_tokens: int
    total_ms: float
    load_ms: float
    prompt_ms: float
    gen_ms: float
    queue_ms: float
    model: str


def _ms(body: dict, key: str) -> float:
    return (body.get(key) or 0) / 1e6


class OllamaClient:
    def __init__(
        self,
        settings: ModelSettings | None = None,
        *,
        sink: CallSink | None = None,
        slot: GenerationSlot | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or ModelSettings()
        self.sink: CallSink = sink if sink is not None else MemorySink()
        self.slot = slot or GenerationSlot(1)
        self._http = httpx.AsyncClient(
            base_url=self.settings.ollama_url,
            timeout=self.settings.timeout_s,
            transport=transport,
            trust_env=False,  # local Ollama: ignore system HTTP(S)_PROXY settings
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OllamaClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = await self._http.post(path, json=payload)
        except httpx.TransportError as e:  # connect, timeout, read errors
            raise ModelUnavailable(f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 500:
            raise ModelUnavailable(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ModelError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise ModelError(f"invalid JSON response: {resp.text[:200]}") from e

    async def _get(self, path: str) -> dict:
        try:
            resp = await self._http.get(path)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            raise ModelUnavailable(f"{type(e).__name__}: {e}") from e

    # ---- embeddings (CPU, not gated by the generation slot) ----
    async def embed(
        self, texts: list[str], *, step: str = "embed", request_id: str | None = None
    ) -> list[list[float]]:
        s = self.settings
        rec = CallRecord(request_id, step, "embed", s.embed_model)
        t0 = time.perf_counter()
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), s.embed_batch):
                batch = texts[i : i + s.embed_batch]
                body = await self._post(
                    "/api/embed",
                    {
                        "model": s.embed_model,
                        "input": batch,
                        "truncate": False,
                        "keep_alive": s.keep_alive,
                        "options": {"num_gpu": s.embed_num_gpu},
                    },
                )
                vecs = body.get("embeddings")
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    raise ModelOutputError("embedding count mismatch", json.dumps(body)[:500])
                for v in vecs:
                    if (
                        not isinstance(v, list)
                        or len(v) != EMBED_DIM
                        or not all(
                            isinstance(x, (int, float)) and math.isfinite(x) for x in v
                        )
                    ):
                        raise ModelOutputError(f"invalid embedding vector (expected {EMBED_DIM} finite floats)")
                out.extend(vecs)
                rec.prompt_tokens += body.get("prompt_eval_count") or 0
                rec.load_ms += _ms(body, "load_duration")
            return out
        except Exception as e:
            rec.ok = False
            rec.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec.total_ms = (time.perf_counter() - t0) * 1000
            self.sink.record(rec)

    # ---- chat (through the generation slot) ----
    async def chat_json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        step: str,
        request_id: str | None = None,
        options: dict | None = None,
        on_position: Callable[[int], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> ChatResult:
        s = self.settings
        rec = CallRecord(request_id, step, "chat", s.chat_model)
        t0 = time.perf_counter()
        payload = {
            "model": s.chat_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": schema,
            "keep_alive": s.keep_alive,
            "options": {
                "temperature": 0,
                "num_ctx": s.num_ctx,
                "num_predict": s.num_predict,
                "seed": 42,
                **(options or {}),
            },
        }
        try:
            async with self.slot.acquire(on_position) as ticket:
                rec.queue_ms = ticket.queue_ms
                if on_start:
                    on_start()
                body = await self._post("/api/chat", payload)
            raw = (body.get("message") or {}).get("content") or ""
            rec.prompt_tokens = body.get("prompt_eval_count") or 0
            rec.completion_tokens = body.get("eval_count") or 0
            rec.load_ms = _ms(body, "load_duration")
            if body.get("done_reason") == "length":
                raise ModelOutputError("output truncated (done_reason=length)", raw)
            try:
                data = json.loads(raw)
            except ValueError as e:
                raise ModelOutputError(f"output is not valid JSON: {e}", raw) from e
            if not isinstance(data, dict):
                raise ModelOutputError("output JSON is not an object", raw)
            result = ChatResult(
                data=data,
                raw=raw,
                prompt_tokens=rec.prompt_tokens,
                completion_tokens=rec.completion_tokens,
                total_ms=_ms(body, "total_duration"),
                load_ms=rec.load_ms,
                prompt_ms=_ms(body, "prompt_eval_duration"),
                gen_ms=_ms(body, "eval_duration"),
                queue_ms=rec.queue_ms,
                model=body.get("model") or s.chat_model,
            )
            return result
        except Exception as e:
            rec.ok = False
            rec.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec.total_ms = (time.perf_counter() - t0) * 1000
            self.sink.record(rec)

    async def health(self) -> dict:
        s = self.settings
        tags = await self._get("/api/tags")
        ps = await self._get("/api/ps")

        def names(b: dict) -> set[str]:
            return {m.get("name") or m.get("model") for m in b.get("models", [])}

        def match(wanted: str, have: set[str]) -> bool:
            return wanted in have or f"{wanted}:latest" in have

        present, loaded = names(tags), names(ps)
        return {
            "chat_present": match(s.chat_model, present),
            "chat_loaded": match(s.chat_model, loaded),
            "embed_present": match(s.embed_model, present),
            "embed_loaded": match(s.embed_model, loaded),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
