from dataclasses import replace

import pytest
from eagle.db.orm import (
    CandidateRecord,
    EpisodeRecord,
    EvidenceRecord,
    IndexJobRecord,
    KnowledgeRecord,
    PreferenceRecord,
)
from sqlalchemy import select

from eagle.domain.enums import CandidateState, CandidateType, PreferenceHardness
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent, UserCorrectionEvent
from eagle.domain.scene import Scene
from eagle.governance import GovernanceService


def episode(*, session_id="s1", tool="wps", fallback_from=None, intervention=False):
    return EpisodeInput(
        user_id="u1",
        session_id=session_id,
        request_text="edit this file",
        scene=Scene(app="office", task="edit", artifact_type="docx"),
        tool_name=tool,
        arguments_digest="digest",
        success=True,
        environment_fingerprint="linux:wps-1",
        fallback_from=fallback_from,
        previous_error_code="E_OPEN" if fallback_from else None,
        user_intervention=intervention,
    )


def test_fallback_does_not_become_preference(session_factory):
    service = GovernanceService(session_factory)

    service.record_episode(episode(tool="libreoffice", fallback_from="wps"))

    with session_factory() as session:
        candidates = list(session.scalars(select(CandidateRecord)))
        knowledge = session.scalar(select(KnowledgeRecord))
        job = session.scalar(select(IndexJobRecord))
    assert [candidate.candidate_type for candidate in candidates] == [CandidateType.KNOWLEDGE.value]
    assert candidates[0].state == CandidateState.PENDING.value
    assert knowledge is None
    assert job is None


def test_repeated_fallback_commits_knowledge_and_outbox_job(session_factory):
    service = GovernanceService(session_factory)
    service.record_episode(episode(session_id="s1", tool="libreoffice", fallback_from="wps"))

    result = service.record_episode(episode(session_id="s2", tool="libreoffice", fallback_from="wps"))

    assert len(result.committed_memory_ids) == 1
    with session_factory() as session:
        knowledge = session.scalar(select(KnowledgeRecord))
        job = session.scalar(select(IndexJobRecord))
    assert knowledge.status == "ACTIVE"
    assert job.memory_id == knowledge.id
    assert job.operation == "UPSERT"


def test_implicit_preference_requires_three_choices_across_sessions(session_factory):
    service = GovernanceService(session_factory)
    service.record_episode(episode(session_id="s1", intervention=True))
    service.record_episode(episode(session_id="s1", intervention=True))

    with session_factory() as session:
        candidate = session.scalar(select(CandidateRecord))
        assert candidate.state == CandidateState.PENDING.value

    result = service.record_episode(episode(session_id="s2", intervention=True))

    assert len(result.committed_memory_ids) == 1
    with session_factory() as session:
        preference = session.scalar(select(PreferenceRecord))
    assert preference.authorization_state == "INFERRED"
    assert preference.hardness == "SOFT"


def test_explicit_hard_preference_commits_once(session_factory):
    service = GovernanceService(session_factory)
    explicit = ExplicitPreferenceEvent(
        key="preferred_tool",
        value={"tool": "wps"},
        hardness=PreferenceHardness.HARD,
        scene=Scene(app="office", task="edit", artifact_type="docx"),
    )

    result = service.record_episode(episode(), explicit_preferences=[explicit])

    assert len(result.committed_memory_ids) == 1
    with session_factory() as session:
        preference = session.scalar(select(PreferenceRecord))
    assert preference.hardness == "HARD"
    assert preference.authorization_state == "EXPLICIT"


def test_explicit_preference_change_creates_new_version(session_factory):
    service = GovernanceService(session_factory)
    first = ExplicitPreferenceEvent(
        key="preferred_tool",
        value={"tool": "wps"},
        hardness=PreferenceHardness.HARD,
        scene=Scene(artifact_type="docx"),
    )
    second = ExplicitPreferenceEvent(
        key="preferred_tool",
        value={"tool": "libreoffice"},
        hardness=PreferenceHardness.HARD,
        scene=Scene(artifact_type="docx"),
    )
    service.record_episode(episode(session_id="s1"), explicit_preferences=[first])
    service.record_episode(episode(session_id="s2"), explicit_preferences=[second])

    with session_factory() as session:
        preferences = list(session.scalars(select(PreferenceRecord).order_by(PreferenceRecord.version)))
    assert [preference.status for preference in preferences] == ["REVOKED", "ACTIVE"]
    assert preferences[1].parent_version_id == preferences[0].id
    assert preferences[1].lineage_id == preferences[0].lineage_id


def test_explicit_evidence_is_not_deduplicated_with_implicit_evidence(session_factory):
    service = GovernanceService(session_factory)
    explicit = ExplicitPreferenceEvent(
        key="preferred_tool",
        value={"tool": "wps"},
        hardness=PreferenceHardness.HARD,
        scene=Scene(app="office", task="edit", artifact_type="docx"),
    )

    result = service.record_episode(
        episode(intervention=True),
        explicit_preferences=[explicit],
    )

    assert len(result.committed_memory_ids) == 1
    with session_factory() as session:
        preference = session.scalar(select(PreferenceRecord))
    assert preference.hardness == "HARD"
    assert preference.authorization_state == "EXPLICIT"


def test_replayed_execution_returns_original_result_without_new_evidence(session_factory):
    service = GovernanceService(session_factory)
    original = episode(tool="libreoffice", fallback_from="wps")

    first = service.record_episode(original)
    replay = service.record_episode(original)

    assert replay == first
    with session_factory() as session:
        assert len(list(session.scalars(select(EpisodeRecord)))) == 1
        assert len(list(session.scalars(select(EvidenceRecord)))) == 1
        assert session.scalar(select(KnowledgeRecord)) is None


def test_execution_id_cannot_be_reused_for_different_payload(session_factory):
    service = GovernanceService(session_factory)
    original = episode()
    service.record_episode(original)

    with pytest.raises(ValueError, match="different Episode payload"):
        service.record_episode(replace(original, tool_name="libreoffice"))


def test_execution_id_scope_is_per_user(session_factory):
    service = GovernanceService(session_factory)
    original = episode()

    first = service.record_episode(original)
    second = service.record_episode(replace(original, user_id="u2"))

    assert second.episode_id != first.episode_id


def test_explicit_user_correction_revokes_committed_preference(session_factory):
    service = GovernanceService(session_factory)
    scene = Scene(app="office", task="edit", artifact_type="docx")
    preference = ExplicitPreferenceEvent(
        key="preferred_tool",
        value={"tool": "wps"},
        hardness=PreferenceHardness.HARD,
        scene=scene,
    )
    service.record_episode(episode(), explicit_preferences=[preference])

    service.record_episode(
        replace(
            episode(session_id="s2"),
            user_correction=UserCorrectionEvent(
                candidate_type=CandidateType.PREFERENCE,
                key="preferred_tool",
                value={"tool": "wps"},
                scene=scene,
            ),
        )
    )

    with session_factory() as session:
        stored = session.scalar(select(PreferenceRecord))
        candidate = session.scalar(select(CandidateRecord))
    assert stored.status == "REVOKED"
    assert candidate.negative_evidence == 1
    assert candidate.state == "REJECTED"
