"""Batch embedding interface for index construction and query vectors."""
import asyncio
import hashlib
import math
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from time import monotonic
from typing import Protocol

from reglament_agent.model import OllamaClient
from reglament_agent.model.settings import ModelSettings

DIMENSION = 1024


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def vector_literal(values: Sequence[float]) -> str:
    if len(values) != DIMENSION or not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("Expected a nonzero vector of 1024 finite values")
    return "[" + ",".join(str(x) for x in values) + "]"


class OllamaEmbedder:
    """Sync adapter for the publisher. The shared client owns batching, validation and telemetry."""
    def __init__(self, client: OllamaClient, runner: asyncio.Runner, *, deadline: float | None = None):
        self.client = client
        self.runner = runner
        self.deadline = deadline

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        request = self.client.embed(list(texts), step="ingest_embed")
        if self.deadline is not None:
            # The async client may send many HTTP batches. Cancel their single parent operation.
            request = asyncio.wait_for(request, max(0.0, self.deadline - monotonic()))
        return self.runner.run(request)


class FakeEmbedder:
    """Deterministic token buckets for offline integration tests."""
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * DIMENSION
            for word in re.findall(r"\w+", text.lower()) or [""]:
                slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % DIMENSION
                vector[slot] += 1.0
            result.append(vector)
        return result


@contextmanager
def open_embedder(kind: str | None = None, *, env=None, deadline: float | None = None) -> Iterator[Embedder]:
    """Share an Ollama connection across batches, optionally ending at a monotonic deadline."""
    env = os.environ if env is None else env
    kind = (kind or env.get("EMBEDDER") or "ollama").lower()
    if kind == "fake":
        yield FakeEmbedder()
        return
    if kind != "ollama":
        raise ValueError(f"Unknown EMBEDDER: {kind}")
    settings = ModelSettings(ollama_url=env.get("MODEL_OLLAMA_URL")
                             or env.get("OLLAMA_URL") or ModelSettings().ollama_url)
    with asyncio.Runner() as runner:
        client = OllamaClient(settings)
        try:
            yield OllamaEmbedder(client, runner, deadline=deadline)
        finally:
            runner.run(client.aclose())


class _SingleUseOllamaEmbedder:
    """Support callers that need one embedding operation without a context manager."""

    def __init__(self, env):
        self.env = env

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with open_embedder("ollama", env=self.env) as embedder:
            return embedder.embed(texts)


def make_embedder(env=None) -> Embedder:
    env = os.environ if env is None else env
    kind = (env.get("EMBEDDER") or "ollama").lower()
    if kind == "fake":
        return FakeEmbedder()
    if kind == "ollama":
        return _SingleUseOllamaEmbedder(env)
    raise ValueError(f"Unknown EMBEDDER: {kind}")
