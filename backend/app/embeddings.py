"""Embedding backends behind one small interface.

`FastEmbedEmbedder` runs a small ONNX model on CPU: no GPU and no extra API key.
`HashEmbedder` is a deterministic, dependency-free stand-in for tests and
offline CI. It only captures token overlap, so do not use it to judge quality.
"""

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        from fastembed import TextEmbedding  # heavy import, keep it lazy

        self._model = TextEmbedding(model_name)
        self.dim = dim

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return list(self._model.embed(list(texts), batch_size=64))

    def embed_query(self, text: str) -> np.ndarray:
        # BGE models expect this instruction prefix on queries (not on documents).
        query = f"Represent this sentence for searching relevant passages: {text}"
        return next(iter(self._model.embed([query])))


class HashEmbedder:
    _TOKEN = re.compile(r"[a-z0-9]+")

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for token in self._TOKEN.findall(text.lower()):
            idx = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "little")
            vec[idx % self.dim] += 1.0
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def create_embedder(backend: str, model_name: str, dim: int) -> Embedder:
    if backend == "fastembed":
        return FastEmbedEmbedder(model_name, dim)
    if backend == "hash":
        return HashEmbedder(dim)
    raise ValueError(f"Unknown embedding backend: {backend}")
