"""Batch embedding interface for index construction and query vectors."""
import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from reglament_agent.model import OllamaClient

DIMENSION = 1024


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def vector_literal(values: Sequence[float]) -> str:
    if len(values) != DIMENSION or not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("Expected a nonzero vector of 1024 finite values")
    return "[" + ",".join(str(x) for x in values) + "]"


class OllamaEmbedder:
    """Sync adapter for the publisher. The shared client owns batching, validation and telemetry."""
    def __init__(self, client: OllamaClient, runner: asyncio.Runner):
        self.client = client
        self.runner = runner

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.runner.run(self.client.embed(list(texts), step="ingest_embed"))


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


def make_embedder(env=None) -> Embedder:
    """EMBEDDER=ollama|fake (default ollama); OLLAMA_URL for the Ollama endpoint."""
    import os
    env = env if env is not None else os.environ
    kind = (env.get("EMBEDDER") or "ollama").lower()
    if kind == "fake":
        return FakeEmbedder()
    if kind != "ollama":
        raise ValueError(f"Unknown EMBEDDER: {kind}")
    client = httpx.Client(base_url=env.get("OLLAMA_URL", "http://localhost:11434"), timeout=300)
    return OllamaEmbedder(client)
