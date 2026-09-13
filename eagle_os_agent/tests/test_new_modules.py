from fastapi import Header
from fastapi.testclient import TestClient
from types import SimpleNamespace
from eagle.api.main import create_app
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.pack.service import PackService
from eagle.db.orm import PreferenceRecord, KnowledgeRecord
from sqlalchemy import select

class GW:
    capability_report = SimpleNamespace(supported=frozenset(), errors=())
    def embedding_ready(self): return True
    def vector_ready(self): return True
    def find_by_index_key(self, *a, **kw): return []
    def search_knowledge(self, **kw): return []

def authenticate(x_user: str | None = Header(default=None)): return x_user
def make_client(sf):
    gw=GW(); gov=GovernanceService(sf)
    app=create_app(session_factory=sf, governance=gov, pack=PackService(sf,gw), forgetting=ForgettingService(sf), gateway=gw, authenticate=authenticate)
    return TestClient(app), gov

def test_sensitive_scrub_collector(session_factory):
    from eagle.domain.events import EpisodeInput
    from eagle.domain.scene import Scene
    gov=GovernanceService(session_factory)
    ep=EpisodeInput(user_id="u1", session_id="s1", request_text="call 13812345678", scene=Scene(app="office"), tool_name="wps", arguments_digest="d", success=True, environment_fingerprint="env")
    gov.record_episode(ep)
    from eagle.db.orm import EpisodeRecord
    with session_factory() as s:
        row=s.scalar(select(EpisodeRecord))
        assert "[PHONE_REDACTED]" in row.request_text
        assert "13812345678" not in row.request_text

def test_nl_forget_api(session_factory):
    client,gov=make_client(session_factory)
    from eagle.domain.events import ExplicitPreferenceEvent
    from eagle.domain.enums import PreferenceHardness
    from eagle.domain.scene import Scene
    from eagle.domain.events import EpisodeInput
    ep=EpisodeInput(user_id="u1", session_id="s1", request_text="x", scene=Scene(artifact_type="docx"), tool_name="wps", arguments_digest="d", success=True, environment_fingerprint="env")
    gov.record_episode(ep, explicit_preferences=[ExplicitPreferenceEvent(key="preferred_tool", value={"tool":"wps"}, hardness=PreferenceHardness.HARD, scene=Scene(artifact_type="docx"))])
    r=client.post("/memories/forget_nl", json={"instruction":"忘掉 wps 相关的偏好"}, headers={"X-User":"u1"})
    assert r.status_code==200
    assert r.json()["targets"]["P"]

def test_multi_source_ingest(session_factory):
    client,_=make_client(session_factory)
    r=client.post("/ingest/behavior", json={"events":[{"user_id":"u1","session_id":"s1","behavior_type":"click","scene":{"app":"office"},"environment_fingerprint":"env"}]}, headers={"X-User":"u1"})
    assert r.status_code==200
    assert r.json()["ingested"]==1
    r2=client.post("/ingest/config", json={"events":[{"user_id":"u1","session_id":"s1","config_key":"preferred_tool","config_value":{"tool":"wps"},"scene":{"artifact_type":"docx"}}]}, headers={"X-User":"u1"})
    assert r2.status_code==200

def test_lifecycle_buffer(session_factory):
    from eagle.lifecycle.short_mid_term import ShortTermBuffer
    buf=ShortTermBuffer(session_id="s1", user_id="u1")
    buf.add({"tool_name":"wps","arguments_digest":"d1","success":True,"environment_fingerprint":"env","scene":{"app":"office"}})
    assert len(buf.episodes)==1

def test_real_clients_fallback():
    from eagle.adapters.kylin.real_embedding import make_embedding_client
    from eagle.adapters.kylin.real_vector import make_vector_client
    e=make_embedding_client()
    v=make_vector_client()
    assert e.healthcheck()
    vec=e.embed(text="hello")
    assert len(vec)==768
