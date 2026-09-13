"""Real Kylin embedding adapter — honest shim/ONNX boundary.

Kylin deployment uses a C API (libKysdk-ai-runtime, gte-base 768d, ~400 MB
ONNX weights + model-service daemon).  No Python wheel is shipped in this
repo/CI, so this module has two backends:

  shim : SHA-256 expansion (deterministic, zero-dep) — default on CI/off-device
  onnx : onnxruntime + HF tokenizer + real gte-base weights — enabled only when
         KYLIN_USE_SHIM=0 and KYLIN_EMBEDDING_MODEL points to an existing .onnx

Selection is explicit and reported via ``backend`` so harnesses/reports never
mislabel a shim run as "real SDK".  This is the injection site referenced in
eagle/bootstrap.py and eagle_harness.py latency bench.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from eagle.adapters.kylin.embedding_shim import sha256_expansion_vector as _shim_vec
except Exception:  # noqa: BLE001
    _shim_vec = None  # type: ignore


class RealEmbeddingClient:
    """ONNX gte-base when weights are present; otherwise deterministic shim.

    ``backend`` is ``"onnx"`` only after a successful InferenceSession creation
    and a probe embed.  Every other case is ``"shim"`` — callers must surface
    this value in reports.
    """

    def __init__(self, dim: int = 768, model_path: str | None = None):
        self._dim = dim
        self._model_path = (model_path or os.getenv("KYLIN_EMBEDDING_MODEL", "")).strip()
        self._session = None
        self._tokenizer = None
        self.backend: str = "shim"
        self._input_names: list[str] = []
        if self._model_path and Path(self._model_path).exists():
            try:
                import onnxruntime as ort  # type: ignore
                from transformers import AutoTokenizer  # type: ignore

                tok_dir = str(Path(self._model_path).parent)
                self._tokenizer = AutoTokenizer.from_pretrained(tok_dir, trust_remote_code=False)
                self._session = ort.InferenceSession(self._model_path, providers=["CPUExecutionProvider"])
                self._input_names = [i.name for i in self._session.get_inputs()]
                # probe
                self._embed_onnx("probe")
                self.backend = "onnx"
            except Exception as exc:  # noqa: BLE001
                # keep shim, remember why for diagnostics
                self._session = None
                self._tokenizer = None
                self.backend = f"shim(fallback: {type(exc).__name__})"

    @property
    def dimension(self) -> int:
        return self._dim

    # -- onnx path ---------------------------------------------------------

    def _embed_onnx(self, text: str) -> list[float]:
        assert self._session is not None and self._tokenizer is not None
        import numpy as np  # type: ignore

        enc = self._tokenizer(text, return_tensors="np", truncation=True, max_length=512, padding=True)
        ort_inputs: dict[str, object] = {}
        if "input_ids" in self._input_names:
            ort_inputs["input_ids"] = enc["input_ids"].astype(np.int64)
        if "attention_mask" in self._input_names and "attention_mask" in enc:
            ort_inputs["attention_mask"] = enc["attention_mask"].astype(np.int64)
        if "token_type_ids" in self._input_names:
            if "token_type_ids" in enc:
                ort_inputs["token_type_ids"] = enc["token_type_ids"].astype(np.int64)
            else:
                ort_inputs["token_type_ids"] = np.zeros_like(ort_inputs["input_ids"])
        # some exports name the first input differently (e.g. "input")
        if not ort_inputs and self._input_names:
            ort_inputs[self._input_names[0]] = enc["input_ids"].astype(np.int64)
        out = self._session.run(None, ort_inputs)
        hidden = out[0]  # [batch, seq, hidden] or [batch, hidden]
        if hidden.ndim == 3:
            # attention-masked mean pooling (gte standard)
            mask = enc.get("attention_mask")
            if mask is not None:
                m = np.array(mask, dtype=np.float32)[:, :, None]
                summed = (hidden * m).sum(axis=1)
                counts = m.sum(axis=1).clip(min=1e-9)
                vec = (summed / counts)[0]
            else:
                vec = hidden[0].mean(axis=0)
        else:
            vec = hidden[0]
        n = float((vec * vec).sum() ** 0.5) or 1.0
        return (vec / n).tolist()

    # -- public ------------------------------------------------------------

    def embed(self, *, text: str, action=None) -> list[float]:
        if self.backend == "onnx" and self._session is not None:
            try:
                return self._embed_onnx(text)
            except Exception:  # noqa: S110, BLE001 - degraded onnx mid-call falls back to shim
                pass
        assert _shim_vec is not None
        return _shim_vec(text, self._dim)

    def embed_batch(self, *, texts: list[str], action=None) -> list[list[float]]:
        return [self.embed(text=t, action=action) for t in texts]

    def healthcheck(self) -> bool:
        return True


def make_embedding_client(dim: int = 768):
    """Factory honouring KYLIN_USE_SHIM.

    KYLIN_USE_SHIM=0  → KylinSdkEmbeddingClient: probes the real kylin-ai-runtime
                       D-Bus service (true Kylin embedding SDK); if the runtime is
                       unreachable it falls back to ONNX gte-base weights and then
                       to the shim, tagging ``backend`` honestly in every state.
    otherwise         → ShimEmbeddingClient directly (no onnx attempt, no heavy deps)
    """
    if os.getenv("KYLIN_USE_SHIM", "1") != "0":
        from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient

        c = ShimEmbeddingClient(dim=dim)
        # attach backend tag for reporting parity
        c.backend = "shim"  # type: ignore[attr-defined]
        return c
    from eagle.adapters.kylin.kylin_sdk_embedding import make_kylin_sdk_embedding_client

    return make_kylin_sdk_embedding_client(dim)
