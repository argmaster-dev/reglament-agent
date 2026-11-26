"""Batch embedding interface for index construction and query vectors."""
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import httpx

DIMENSION = 1024


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def vector_literal(values: Sequence[float]) -> str:
    if len(values) != DIMENSION or not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("Expected a nonzero vector of 1024 finite values")
    return "[" + ",".join(str(x) for x in values) + "]"


class OllamaEmbedder:
    def __init__(self, client: httpx.Client, model: str = "bge-m3", batch_size: int = 16):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.client, self.model, self.batch_size = client, model, batch_size

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result = []
        for i in range(0, len(texts), self.batch_size):
            batch = list(texts[i:i + self.batch_size])
            response = self.client.post("/api/embed", json={
                "model": self.model, "input": batch, "options": {"num_gpu": 0}, "truncate": False,
            })
            response.raise_for_status()
            vectors = response.json()["embeddings"]
            if len(vectors) != len(batch):
                raise ValueError("Ollama returned a different number of embeddings")
            result.extend(vectors)
        return result


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
