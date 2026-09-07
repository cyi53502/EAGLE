from types import SimpleNamespace

from fastapi import Header
from fastapi.testclient import TestClient

from eagle.api.main import create_app
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.pack.service import PackService


class Gateway:
    capability_report = SimpleNamespace(supported=frozenset(), errors=())

    def embedding_ready(self):
        return True

    def vector_ready(self):
        return True

    def find_by_index_key(self, user_id, index_key):
        return []

    def search_knowledge(self, **kwargs):
        return []


def authenticate(x_user: str | None = Header(default=None)):
    return x_user


def client(session_factory):
    gateway = Gateway()
    governance = GovernanceService(session_factory)
    app = create_app(
        session_factory=session_factory,
        governance=governance,
        pack=PackService(session_factory, gateway),
        forgetting=ForgettingService(session_factory),
        gateway=gateway,
        authenticate=authenticate,
    )
    return TestClient(app)


def episode_payload(**overrides):
    payload = {
        "execution_id": "execution-1",
        "session_id": "session-1",
        "request_text": "edit this file",
        "scene": {"app": "office", "task": "edit", "artifact_type": "docx"},
        "tool_name": "wps",
        "arguments_digest": "digest",
        "success": True,
        "environment_fingerprint": "linux:wps-1",
        "explicit_preferences": [
            {
                "key": "preferred_tool",
                "value": {"tool": "wps"},
                "hardness": "HARD",
                "scene": {"artifact_type": "docx"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_api_requires_authentication_and_uses_authenticated_tenant(session_factory):
    api = client(session_factory)

    assert api.post("/episodes", json=episode_payload()).status_code == 401
    assert (
        api.post(
            "/episodes",
            json=episode_payload(),
            headers={"X-User": "u1"},
        ).status_code
        == 200
    )

    own = api.get("/preferences", headers={"X-User": "u1"})
    other = api.get("/preferences", headers={"X-User": "u2"})
    assert len(own.json()) == 1
    assert other.json() == []


def test_api_episode_replay_is_idempotent_and_payload_change_conflicts(session_factory):
    api = client(session_factory)
    headers = {"X-User": "u1"}

    first = api.post("/episodes", json=episode_payload(), headers=headers)
    replay = api.post("/episodes", json=episode_payload(), headers=headers)
    changed = api.post(
        "/episodes",
        json=episode_payload(tool_name="libreoffice"),
        headers=headers,
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert changed.status_code == 409


def test_api_rejects_incomplete_fallback_evidence(session_factory):
    api = client(session_factory)

    response = api.post(
        "/episodes",
        json=episode_payload(fallback_from="wps", previous_error_code=None),
        headers={"X-User": "u1"},
    )

    assert response.status_code == 422
