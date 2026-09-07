import pytest
from sqlalchemy import select
from test_governance import episode

from eagle.conflict.service import ConflictService
from eagle.db.orm import KnowledgeRecord, KnowledgeRevalidationRecord, PreferenceRecord
from eagle.domain.enums import PreferenceHardness
from eagle.domain.events import ExplicitPreferenceEvent
from eagle.domain.scene import Scene
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.pack.service import PackService
from eagle.preference.compiler import NO_FEASIBLE_ACTION, apply_constraints


class SearchGateway:
    def __init__(self):
        self.results = []
        self.last_search = None

    def search_knowledge(self, **kwargs):
        self.last_search = kwargs
        return self.results


class Tool:
    def __init__(self, name, requires_network):
        self.name = name
        self.requires_network = requires_network


def test_hard_preference_compiles_to_tool_constraint(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(
        episode(),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key="preferred_tool",
                value={"tool": "wps"},
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )

    context = PackService(session_factory, SearchGateway()).build(
        query="edit",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    assert context.constraints.allowed_tools == frozenset({"wps"})
    tools = [Tool("wps", False), Tool("libreoffice", False), Tool("online-converter", True)]
    assert [tool.name for tool in apply_constraints(tools, context.constraints)] == ["wps"]


def test_hard_constraints_return_no_feasible_action_signal(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(
        episode(),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key="preferred_tool",
                value={"tool": "wps"},
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )
    context = PackService(session_factory, SearchGateway()).build(
        query="edit",
        user_id="u1",
        scene=Scene(artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    result = apply_constraints([Tool("libreoffice", False)], context.constraints)

    assert result is NO_FEASIBLE_ACTION


def test_forgetting_is_immediately_invisible_before_vector_delete(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory.begin() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        knowledge.mem0_id = "vector-1"
        knowledge_id = knowledge.id

    gateway = SearchGateway()
    gateway.results = [
        {
            "id": "vector-1",
            "score": 0.99,
            "metadata": {"eagle_memory_id": knowledge_id},
        }
    ]
    ForgettingService(session_factory).forget_knowledge(knowledge_id, user_id="u1")

    context = PackService(session_factory, gateway).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    assert context.knowledge == ()


def test_environment_drift_blocks_and_marks_knowledge(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        knowledge_id = knowledge.id
    with session_factory.begin() as session:
        session.get(KnowledgeRecord, knowledge_id).mem0_id = "vector-1"

    gateway = SearchGateway()
    gateway.results = [{"id": "vector-1", "score": 0.99, "metadata": {"eagle_memory_id": knowledge_id}}]
    context = PackService(session_factory, gateway).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-2",
    )

    assert context.knowledge == ()
    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
        request = session.scalar(select(KnowledgeRevalidationRecord))
    assert knowledge.status == "ACTIVE"
    assert request.target_environment == "linux:wps-2"


def test_pk_visibility_masks_without_deleting_knowledge(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(
        episode(session_id="s3"),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key="require_offline",
                value={"required": True},
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )
    with session_factory.begin() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        preference = session.scalar(select(PreferenceRecord))
        knowledge.mem0_id = "vector-1"
        ConflictService().mask_knowledge(
            session,
            preference_id=preference.id,
            knowledge_id=knowledge.id,
            reason="knowledge requires network",
        )
        knowledge_id = knowledge.id

    gateway = SearchGateway()
    gateway.results = [{"id": "vector-1", "score": 0.99, "metadata": {"eagle_memory_id": knowledge_id}}]
    context = PackService(session_factory, gateway).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    assert context.knowledge == ()
    with session_factory() as session:
        knowledge = session.get(KnowledgeRecord, knowledge_id)
    assert knowledge.status == "ACTIVE"


def test_forgotten_preference_stops_constraining_pack(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(
        episode(),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key="preferred_tool",
                value={"tool": "wps"},
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )
    with session_factory() as session:
        preference_id = session.scalar(select(PreferenceRecord.id))

    ForgettingService(session_factory).forget_preference(preference_id, user_id="u1")
    context = PackService(session_factory, SearchGateway()).build(
        query="edit",
        user_id="u1",
        scene=Scene(artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    assert context.constraints.allowed_tools is None


def test_pack_collapses_duplicate_vectors_for_same_knowledge(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))
    with session_factory.begin() as session:
        session.get(KnowledgeRecord, knowledge_id).mem0_id = "vector-a"

    gateway = SearchGateway()
    gateway.results = [
        {"id": "vector-a", "score": 0.9, "metadata": {"eagle_memory_id": knowledge_id}},
        {"id": "vector-b", "score": 0.8, "metadata": {"eagle_memory_id": knowledge_id}},
    ]

    context = PackService(session_factory, gateway).build(
        query="fallback",
        user_id="u1",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
    )

    assert len(context.knowledge) == 1
    assert context.knowledge[0]["id"] == knowledge_id
    assert gateway.last_search["eligible_memory_ids"] == ("vector-a",)


@pytest.mark.parametrize(
    "key,value",
    [
        ("allowed_formats", {"formats": ["pdf"]}),
        ("privacy_rule", {"rule": "no-network-for-sensitive"}),
    ],
)
def test_unenforceable_hard_preference_fails_compilation(session_factory, key, value):
    governance = GovernanceService(session_factory)
    governance.record_episode(
        episode(),
        explicit_preferences=[
            ExplicitPreferenceEvent(
                key=key,
                value=value,
                hardness=PreferenceHardness.HARD,
                scene=Scene(artifact_type="docx"),
            )
        ],
    )

    with pytest.raises(ValueError, match="no executable constraint adapter"):
        PackService(session_factory, SearchGateway()).build(
            query="edit",
            user_id="u1",
            scene=Scene(artifact_type="docx"),
            environment_fingerprint="linux:wps-1",
        )


def test_forgetting_is_scoped_to_authenticated_user(session_factory):
    governance = GovernanceService(session_factory)
    governance.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))
    governance.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))
    with session_factory() as session:
        knowledge_id = session.scalar(select(KnowledgeRecord.id))

    with pytest.raises(ValueError, match="not found"):
        ForgettingService(session_factory).forget_knowledge(
            knowledge_id,
            user_id="another-user",
        )

    with session_factory() as session:
        assert session.get(KnowledgeRecord, knowledge_id).status == "ACTIVE"
