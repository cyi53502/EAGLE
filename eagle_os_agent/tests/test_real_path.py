"""Real-path tests for the Shim -> Real switch.

These exercise ``make_vector_client`` / ``make_embedding_client`` /
``create_auto_gateway`` honouring ``KYLIN_USE_SHIM``.  Everything that needs
the live ``kylin-ai-vector-engine`` daemon (or the ONNX weights) skips cleanly
when it is not available, so the default (shim) CI run stays green and the
real path is still exercised where the daemon is running.
"""
import os

import pytest

from eagle.adapters.kylin import kylin_sdk_embedding as kse
from eagle.adapters.kylin.capabilities import (
    REQUIRED_FOR_GATEWAY,
    probe_vector_capabilities,
)
from eagle.adapters.kylin.kylin_sdk_embedding import KylinSdkEmbeddingClient, KylinSdkError
from eagle.adapters.kylin.real_embedding import make_embedding_client
from eagle.adapters.kylin.real_vector import RealVectorClient, make_vector_client
from eagle.adapters.kylin.vector_shim import ShimVectorClient

_GTE_MODEL = "/root/rivermind-data/models/kylin-embedding/model.onnx"


def _real_vector_or_none():
    """Return a live RealVectorClient, or None (bridge missing / daemon down)."""
    try:
        return RealVectorClient()
    except Exception:  # noqa: BLE001 - external SDK boundary
        return None


def _require_real_vector():
    client = _real_vector_or_none()
    if client is None:
        pytest.skip("kylin-ai-vector-engine daemon or bridge not available")
    return client


# ---------------------------------------------------------------------------
# env-driven selection (no daemon required)
# ---------------------------------------------------------------------------

def test_default_env_selects_shims(monkeypatch):
    monkeypatch.delenv("KYLIN_USE_SHIM", raising=False)
    vec = make_vector_client()
    assert isinstance(vec, ShimVectorClient)
    assert vec.backend == "shim"


def test_kylin_use_shim_1_selects_shims(monkeypatch):
    monkeypatch.setenv("KYLIN_USE_SHIM", "1")
    vec = make_vector_client()
    assert isinstance(vec, ShimVectorClient)
    assert vec.backend == "shim"


def test_real_request_falls_back_to_shim_with_honest_backend(monkeypatch):
    """KYLIN_USE_SHIM=0 with an unreachable UDS must NOT raise and must NOT
    mislabel itself as real.  ``_connect`` is patched so the test stays fast:
    the real connect-failure path (SDK retries 5x with backoff) is exercised
    by the daemon-gated tests, not by every suite run."""
    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    monkeypatch.setenv("KYLIN_VECTOR_UDS", "/tmp/definitely-not-a-kylin-socket")

    def boom(self):
        raise RuntimeError("test: engine unreachable")

    monkeypatch.setattr(RealVectorClient, "_connect", boom)
    vec = make_vector_client()
    assert isinstance(vec, ShimVectorClient)
    assert vec.backend.startswith("shim(fallback:")


# ---------------------------------------------------------------------------
# real vector path (needs the daemon)
# ---------------------------------------------------------------------------

def test_real_vector_client_roundtrip(monkeypatch):
    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    client = _require_real_vector()
    assert client.backend == "real"
    name = "eagle_test_real_roundtrip"
    client.delete_collection(name=name)
    client.ensure_collection(name=name, dimension=4, metric="cosine_distance")
    try:
        client.upsert(
            collection=name,
            vectors=[[1.0, 0.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0]],
            payloads=[{"tag": "a"}, {"tag": "b"}],
            ids=["rt-a", "rt-b"],
        )
        rows = client.search(collection=name, vector=[1.0, 0.0, 0.0, 0.0], limit=2, filters={})
        # engine returns cosine similarity best-first; we expose cosine distance
        assert [r.id for r in rows] == ["rt-a", "rt-b"]
        assert rows[0].score == pytest.approx(0.0, abs=1e-6)
        assert rows[1].score == pytest.approx(2.0, abs=1e-6)
        got = client.get(collection=name, vector_id="rt-a", include_vector=False)
        assert got is not None and got.payload["tag"] == "a"
    finally:
        client.delete_collection(name=name)


def test_real_capability_probe_passes_required_gate(monkeypatch):
    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    client = _require_real_vector()
    report = probe_vector_capabilities(client, dimension=4, metric="cosine_distance")
    assert report.errors == ()
    assert REQUIRED_FOR_GATEWAY <= report.supported


# ---------------------------------------------------------------------------
# real embedding path (needs ONNX weights)
# ---------------------------------------------------------------------------

def test_embedding_client_backend_onnx_when_weights_present(monkeypatch):
    model = os.getenv("KYLIN_EMBEDDING_MODEL", "")
    if not model:
        model = _GTE_MODEL
    if not os.path.exists(model):
        pytest.skip("kylin gte-base weights not present")
    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", model)
    client = make_embedding_client(768)
    # the real path first probes the kylin-ai-runtime SDK tier; without a live
    # runtime it degrades to ONNX and says so: "onnx(fallback: no ... socket)"
    assert client.backend.startswith("onnx")
    vec = client.embed(text="hello")
    assert len(vec) == 768


def test_embedding_client_shim_without_weights(monkeypatch):
    """No onnx weights -> honest plain "shim" (no failed-onnx fallback tag,
    because onnx was never attempted).  With the SDK tier probing first the tag
    carries the sdk-unavailable reason, never a claim that onnx ran."""
    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", "/tmp/no-such-model.onnx")
    client = make_embedding_client(768)
    assert client.backend.startswith("shim")
    assert "no kylin-ai-runtime socket" in client.backend


# ---------------------------------------------------------------------------
# Kylin SDK embedding tier (protocol-driven; no live runtime/daemon needed)
# ---------------------------------------------------------------------------

def _mock_runtime_socket(monkeypatch, tmp_path, *, exists: bool = True) -> str:
    sock = tmp_path / "core-textembedding.sock"
    if exists:
        sock.touch()
    monkeypatch.setattr(kse, "runtime_socket_path", lambda uid=None: str(sock))
    return str(sock)


def _sdk_protocol_fake(calls: dict):
    """A KylinSdkEmbeddingClient._dbus_call stand-in speaking the documented
    protocol: Init -> (sessionId, 0); EmbeddingText -> JSON string with
    vector_result; GetModelInfo -> text_model JSON."""

    def fake(self, method, *args, **kwargs):  # bound as instance attribute
        calls[method] = calls.get(method, 0) + 1
        if method == "Init":
            return (42, 0)
        if method == "EmbeddingText":
            return ('{"errorCode":0,"errorMessage":"Success","vector_result":[3.0,4.0]}',)
        if method == "GetModelInfo":
            return ('{"models":{"text_model":{"dim":768,"name":"ensemble_gte_base_uint8_text","ondevice":true}}}',)
        raise AssertionError(f"unexpected method {method}")

    return fake


def test_kylin_sdk_client_uses_sdk_when_protocol_answers(monkeypatch, tmp_path):
    """Live (mocked) runtime -> backend "kylin-sdk", normalized vector, model info."""
    _mock_runtime_socket(monkeypatch, tmp_path)
    monkeypatch.setenv("KYLIN_EMBEDDING_SDK", "1")
    calls: dict = {}
    monkeypatch.setattr(KylinSdkEmbeddingClient, "_dbus_call", _sdk_protocol_fake(calls))
    client = KylinSdkEmbeddingClient(768)  # __init__ probes through the mock protocol
    assert client.backend == "kylin-sdk"
    assert calls["Init"] == 1
    assert client.dimension == 768
    assert client.model_info is not None
    assert client.model_info["dim"] == 768
    vec = client.embed(text="编辑文档")
    # [3,4] -> L2-normalized [0.6, 0.8]
    assert vec == pytest.approx([0.6, 0.8], abs=1e-6)
    assert client.healthcheck() is True


def test_kylin_sdk_client_reinit_after_runtime_error(monkeypatch, tmp_path):
    """A crashed runtime (EmbeddingText raises) is recovered by re-Init'ing the
    session — mirroring the upstream client's own reconnect behaviour."""
    _mock_runtime_socket(monkeypatch, tmp_path)
    monkeypatch.setenv("KYLIN_EMBEDDING_SDK", "1")
    calls: dict = {}

    def fake(self, method, *args, **kwargs):
        calls[method] = calls.get(method, 0) + 1
        if method == "Init":
            return (100 * calls[method], 0)
        if method == "EmbeddingText":
            if calls[method] == 1:
                raise KylinSdkError("runtime crashed")
            return ('{"errorCode":0,"vector_result":[1.0,0.0]}',)
        if method == "GetModelInfo":
            return ('{"models":{"text_model":{"dim":768}}}',)
        raise AssertionError(method)

    monkeypatch.setattr(KylinSdkEmbeddingClient, "_dbus_call", fake)
    client = KylinSdkEmbeddingClient(768)  # __init__ probes through the mock protocol
    assert client.backend == "kylin-sdk"
    vec = client.embed(text="x")
    assert calls["Init"] == 2  # initial + reconnect
    assert vec == pytest.approx([1.0, 0.0], abs=1e-6)


def test_kylin_sdk_client_honest_fallback_without_socket(monkeypatch, tmp_path):
    """No runtime socket -> honest "shim(fallback: no kylin-ai-runtime socket ...)",
    never a claim that the SDK ran."""
    _mock_runtime_socket(monkeypatch, tmp_path, exists=False)
    monkeypatch.setenv("KYLIN_EMBEDDING_SDK", "1")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", "/tmp/no-such-model.onnx")
    client = KylinSdkEmbeddingClient(768)
    assert client.backend.startswith("shim(fallback:")
    assert "no kylin-ai-runtime socket" in client.backend
    vec = client.embed(text="hi")
    assert len(vec) == 768  # shim keeps serving


def test_kylin_sdk_client_onnx_fallback_tagged_when_weights_present(monkeypatch, tmp_path):
    model = os.getenv("KYLIN_EMBEDDING_MODEL", "") or _GTE_MODEL
    if not os.path.exists(model):
        pytest.skip("kylin gte-base weights not present")
    _mock_runtime_socket(monkeypatch, tmp_path, exists=False)
    monkeypatch.setenv("KYLIN_EMBEDDING_SDK", "1")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", model)
    client = KylinSdkEmbeddingClient(768)
    assert client.backend.startswith("onnx(fallback:")
    assert "no kylin-ai-runtime socket" in client.backend
    vec = client.embed(text="hello")
    assert len(vec) == 768


def test_kylin_embedding_sdk_0_preserves_legacy_backend(monkeypatch, tmp_path):
    """KYLIN_EMBEDDING_SDK=0 disables the probe entirely; backend is exactly the
    ONNX/shim state with no sdk-unavailable suffix."""
    _mock_runtime_socket(monkeypatch, tmp_path, exists=True)
    monkeypatch.setenv("KYLIN_EMBEDDING_SDK", "0")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", "/tmp/no-such-model.onnx")
    client = KylinSdkEmbeddingClient(768)
    assert client.backend == "shim"


# ---------------------------------------------------------------------------
# end-to-end auto gateway (needs daemon + weights)
# ---------------------------------------------------------------------------

def test_auto_gateway_reports_real_backend(monkeypatch, tmp_path):
    model = os.getenv("KYLIN_EMBEDDING_MODEL", "")
    if not model:
        model = _GTE_MODEL
    if not os.path.exists(model):
        pytest.skip("kylin gte-base weights not present")
    client = _require_real_vector()
    client.delete_collection(name="eagle_memories")

    from eagle.bootstrap import create_auto_gateway

    monkeypatch.setenv("KYLIN_USE_SHIM", "0")
    monkeypatch.setenv("KYLIN_EMBEDDING_MODEL", model)
    gw = create_auto_gateway(history_db_path=str(tmp_path / "h.db"))
    assert gw.backend["vector"] == "real"
    # embedding probes the SDK tier first and honestly tags the ONNX fallback
    assert gw.backend["embedding"].startswith("onnx")
    assert gw.embedding_ready() and gw.vector_ready()
