"""Kylin SDK text-embedding adapter — real kylin-ai-runtime D-Bus, ONNX fallback.

Speaks the *real* Kylin AI SDK wire protocol so EAGLE genuinely calls the
galaxy-Kylin embedding SDK when the runtime is present, and degrades to the
ONNX path otherwise with an honest ``backend`` tag.

The kylin-ai-runtime daemon exports ``com.kylin.AiRuntime.CoreTextEmbeddingService``
on a per-uid unix socket (not the session bus):

    address    unix:path=/tmp/.kylin-ai-runtime-unix/<uid>/core-textembedding.sock
    object     /com/kylin/AiRuntime/CoreTextEmbeddingService
    interface  com.kylin.AiRuntime.CoreTextEmbeddingService

    Init          {"engineName":"Embedding"}                  -> (sessionId:int, errorCode:int)
    EmbeddingText {"text":..., "sessionId":N}                 -> '{"errorCode":0,"errorMessage":"Success","vector_result":[...]}'
    GetModelInfo  sessionId                                   -> '{"models":{"text_model":{...}}}'

This mirrors the open-source Kylin client (kylin-team/libkysdk-coreai-speech,
src/embedding/textembeddingprocessorproxy.cpp) and the runtime service source
(kylin-team/kylin-ai-runtime, src/services/coreai/textembedding/).  Engine name
is literally ``"Embedding"``; model info is ``dim 768 / ensemble_gte_base_uint8_text``.

Selection (env ``KYLIN_EMBEDDING_SDK``):
  "0"  -> never probe the SDK (pure ONNX/shim path, legacy behaviour)
  "1"  -> force the SDK tier (probe always; still falls back to ONNX with an
          honest tag if the runtime is unreachable)
  unset-> auto: probe the runtime socket; use the SDK when it answers
"""
from __future__ import annotations

import ast
import json
import math
import os
import shutil
import subprocess

# -- protocol constants (verified against the open-source Kylin client/runtime) --
_RUNTIME_SOCK_REL = ".kylin-ai-runtime-unix"
_RUNTIME_SOCK_FILE = "core-textembedding.sock"
OBJECT_PATH = "/com/kylin/AiRuntime/CoreTextEmbeddingService"
INTERFACE = "com.kylin.AiRuntime.CoreTextEmbeddingService"
ENGINE_NAME = os.getenv("KYLIN_SDK_EMBEDDING_ENGINE", "Embedding")


def runtime_socket_path(uid: int | None = None) -> str:
    """Absolute path of the kylin-ai-runtime text-embedding socket for ``uid``."""
    uid = os.getuid() if uid is None else uid
    return f"/tmp/{_RUNTIME_SOCK_REL}/{uid}/{_RUNTIME_SOCK_FILE}"


class KylinSdkError(RuntimeError):
    """The kylin-ai-runtime did not answer the embedding protocol."""


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class KylinSdkEmbeddingClient:
    """Embedding client: kylin-ai-runtime D-Bus when reachable, else ONNX.

    ``backend`` is honest in every state:
      "kylin-sdk"                        runtime connected and session initialised
      "onnx(fallback: <reason>)"         runtime absent/failed, ONNX weights used
      "shim(fallback: <reason>)"         runtime absent/failed, no weights -> shim
      "shim(fallback: OnnxError)"        fallback's own degradation preserved
    """

    def __init__(self, dim: int = 768, *, timeout_ms: int = 15_000, gdbus: str | None = None):
        self._dim = dim
        self._timeout_ms = timeout_ms
        self._sock_addr = f"unix:path={runtime_socket_path()}"
        self._session_id: int | None = None
        self._model_info: dict | None = None
        self._sdk_reason: str | None = None
        self.backend: str = "shim"
        self._fallback = None  # RealEmbeddingClient (lazy) when the SDK tier is down

        self._gdbus = gdbus if gdbus is not None else shutil.which("gdbus")
        if os.getenv("KYLIN_EMBEDDING_SDK", "") == "0":
            # legacy mode: never probe the SDK; backend is exactly the ONNX/shim state
            self._sdk_reason = None
            self._fallback = self._make_fallback()
            self.backend = self._compose_backend()
            return

        self._try_connect()

    # -- construction ---------------------------------------------------------

    def _make_fallback(self):
        from eagle.adapters.kylin.real_embedding import RealEmbeddingClient

        return RealEmbeddingClient(dim=self._dim)

    def _compose_backend(self) -> str:
        fb = getattr(self._fallback, "backend", "shim")
        if self._sdk_reason is None:
            return fb  # fallback's own state already describes what happened
        if fb.startswith("shim(fallback:"):
            # fallback is itself degraded; keep the more specific inner reason
            return fb
        return f"{fb}(fallback: {self._sdk_reason})"

    def _try_connect(self) -> None:
        sock = runtime_socket_path()
        if not os.path.exists(sock):
            self._sdk_reason = f"no kylin-ai-runtime socket at {sock}"
            self._fallback = self._make_fallback()
            self.backend = self._compose_backend()
            return
        if not self._gdbus:
            self._sdk_reason = "gdbus not available"
            self._fallback = self._make_fallback()
            self.backend = self._compose_backend()
            return
        try:
            session_id, err = self._dbus_call("Init", json.dumps({"engineName": ENGINE_NAME}))
            if not session_id or session_id <= 0 or err:
                raise KylinSdkError(f"Init failed sessionId={session_id} errorCode={err}")
            self._session_id = int(session_id)
            self.backend = "kylin-sdk"
            try:
                self._model_info = self._get_model_info()
            except Exception:  # noqa: BLE001 - model info is diagnostic only
                self._model_info = None
        except Exception as exc:  # noqa: BLE001 - external SDK boundary
            self._sdk_reason = f"kylin-ai-runtime unavailable: {type(exc).__name__}"
            self._fallback = self._make_fallback()
            self.backend = self._compose_backend()

    # -- D-Bus transport (monkeypatchable for tests) --------------------------

    def _dbus_call(self, method: str, *args, timeout_ms: int | None = None) -> object:
        """Call a method on the runtime over the unix-socket D-Bus, return the
        decoded GVariant reply (tuple for multiple out-args)."""
        cmd = [
            self._gdbus, "call", "--address", self._sock_addr,
            "--object-path", OBJECT_PATH,
            "--method", f"{INTERFACE}.{method}",
            "--timeout", str(timeout_ms or self._timeout_ms),
            *args,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=(timeout_ms or self._timeout_ms) / 1000 + 5,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise KylinSdkError(f"gdbus {method} failed to run: {exc}") from exc
        if proc.returncode != 0:
            raise KylinSdkError(f"gdbus {method} failed: {(proc.stderr or proc.stdout).strip()}")
        try:
            return ast.literal_eval(proc.stdout.strip())
        except (ValueError, SyntaxError) as exc:
            raise KylinSdkError(f"gdbus {method} unparseable reply: {proc.stdout[:200]!r}") from exc

    def _reinit_session(self) -> None:
        session_id, err = self._dbus_call("Init", json.dumps({"engineName": ENGINE_NAME}))
        if not session_id or session_id <= 0 or err:
            raise KylinSdkError(f"re-Init failed sessionId={session_id} errorCode={err}")
        self._session_id = int(session_id)

    def _embed_sdk(self, text: str) -> list[float]:
        payload = json.dumps({"text": text, "sessionId": self._session_id})
        try:
            (resp_json,) = self._dbus_call("EmbeddingText", payload)  # reply is a single string
        except KylinSdkError:
            # runtime may have crashed; its own client recreates the session
            self._reinit_session()
            (resp_json,) = self._dbus_call("EmbeddingText", json.dumps({"text": text, "sessionId": self._session_id}))
        resp = json.loads(resp_json)
        if resp.get("errorCode", -1) != 0 or "vector_result" not in resp:
            raise KylinSdkError(resp.get("errorMessage", "empty embedding result"))
        return _l2_normalize([float(v) for v in resp["vector_result"]])

    def _get_model_info(self) -> dict:
        (resp_json,) = self._dbus_call("GetModelInfo", str(self._session_id))
        resp = json.loads(resp_json)
        return resp.get("models", {}).get("text_model", resp)

    # -- client protocol ------------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def model_info(self) -> dict | None:
        """Text-embedding model info from the real runtime (``{"dim", "name", ...}``)."""
        return self._model_info

    def embed(self, *, text: str, action=None) -> list[float]:
        if self.backend == "kylin-sdk":
            return self._embed_sdk(text)
        assert self._fallback is not None
        return self._fallback.embed(text=text, action=action)

    def embed_batch(self, *, texts: list[str], action=None) -> list[list[float]]:
        if self.backend == "kylin-sdk":
            return [self._embed_sdk(t) for t in texts]
        assert self._fallback is not None
        return self._fallback.embed_batch(texts=texts, action=action)

    def healthcheck(self) -> bool:
        if self.backend == "kylin-sdk":
            return True
        assert self._fallback is not None
        return self._fallback.healthcheck()


def make_kylin_sdk_embedding_client(dim: int = 768) -> KylinSdkEmbeddingClient:
    """Factory for the Kylin SDK embedding tier (env ``KYLIN_EMBEDDING_SDK``)."""
    return KylinSdkEmbeddingClient(dim=dim)
