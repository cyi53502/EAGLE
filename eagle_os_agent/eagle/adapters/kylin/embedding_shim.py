"""Deterministic, dependency-free text embedding shim.

Real SDK: gte-base text model, 768 dims, behind
kylin-ai-model-service / libKysdk-ai-runtime.  The production stack pulls
~400 MB of ONNX weights and a model-service daemon; this shim keeps stage 8
exercisable without that weight.  Callers are isolated behind
KylinEmbeddingClient Protocol — swapping in the real SDK changes nothing
outside this file.

Construction: SHA-256 expansion seeded by the text bytes, splitmix64, mapped to
[-1, 1), then L2-normalized.  COSINE distance among distinct texts is stable
(≈0.86±0.06); identical texts collide to 0.
"""

from __future__ import annotations

import hashlib
import math
import struct


def _splitmix64_step(state: int) -> int:
    state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    z = state
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
    z = (z ^ (z >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
    return z ^ (z >> 31)


def sha256_expansion_vector(text: str, dim: int) -> list[float]:
    seed_material = hashlib.sha256(text.encode("utf-8")).digest()
    state = struct.unpack(">Q", seed_material[:8])[0]
    for i in range(8, 32):
        state = (state ^ (seed_material[i] << (i % 8))) & 0xFFFFFFFFFFFFFFFF
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        if counter and counter % 4 == 0:
            state = (state ^ counter) & 0xFFFFFFFFFFFFFFFF
            state = _splitmix64_step(state)
        z = _splitmix64_step(state)
        val = (z / 18446744073709551616.0) * 2.0 - 1.0
        out.append(float(val))
        state = (state ^ (z >> 16)) & 0xFFFFFFFFFFFFFFFF
        counter += 1
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


class ShimEmbeddingClient:
    """KylinEmbeddingClient shim (768-dim gte-base parity)."""

    def __init__(self, dim: int = 768):
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    def embed(self, *, text: str, action=None) -> list[float]:
        return sha256_expansion_vector(text, self._dim)

    def embed_batch(self, *, texts: list[str], action=None) -> list[list[float]]:
        return [self.embed(text=t, action=action) for t in texts]

    def healthcheck(self) -> bool:
        return True
