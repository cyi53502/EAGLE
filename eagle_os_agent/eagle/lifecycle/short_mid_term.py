"""Short-term / mid-term / long-term memory lifecycle (赛题 §1-6).

Short  = session-buffer (TTL, in-DB episodes not yet candidate)
Mid    = CandidateRecord PENDING (cross-session, not yet committed)
Long   = PreferenceRecord / KnowledgeRecord ACTIVE

Transitions:
  session_to_candidate  : flush short buffer → candidate via GovernanceService
  cross_session_merge   : dedup/merge mid candidates across sessions (MSC/LoCoMo)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from eagle.db.orm import CandidateRecord, EpisodeRecord


@dataclass
class ShortTermBuffer:
    """Per-session in-memory buffer before governance commit."""
    session_id: str
    user_id: str
    episodes: list[dict] = field(default_factory=list)
    ttl_s: int = 1800

    def add(self, episode_dict: dict) -> None:
        self.episodes.append(episode_dict)

    def flush(self, governance, scene_factory=None) -> list[str]:
        """Flush buffered episodes through governance; return committed ids."""
        from eagle.domain.events import EpisodeInput
        from eagle.domain.scene import Scene
        committed: list[str] = []
        for raw in self.episodes:
            ep = EpisodeInput(
                user_id=self.user_id, session_id=self.session_id,
                request_text=raw.get("request_text",""), scene=scene_factory(raw) if scene_factory else Scene.from_dict(raw.get("scene")),
                tool_name=raw["tool_name"], arguments_digest=raw["arguments_digest"],
                success=raw.get("success", True), environment_fingerprint=raw.get("environment_fingerprint",""),
                execution_id=raw.get("execution_id") or __import__("uuid").uuid4().hex,
            )
            res = governance.record_episode(ep)
            committed.extend(res.committed_memory_ids)
        self.episodes.clear()
        return committed


def session_to_candidate(session_factory, user_id: str, session_id: str) -> dict:
    """Summarize short→mid transition for a session (DB-level)."""
    with session_factory() as session:
        eps = list(session.scalars(select(EpisodeRecord).where(
            EpisodeRecord.user_id == user_id, EpisodeRecord.session_id == session_id
        )))
        cands = list(session.scalars(select(CandidateRecord).where(CandidateRecord.user_id == user_id)))
        linked = sum(1 for c in cands if any(
            session.scalar(select(EpisodeRecord.id).where(EpisodeRecord.id == e.id)) for e in eps
        ))
        return {"episodes": len(eps), "candidates": len(cands), "linked": linked}


def cross_session_merge(session_factory, user_id: str) -> dict:
    """Mid-term dedup: report duplicate identities across sessions (deterministic)."""
    with session_factory() as session:
        cands = list(session.scalars(select(CandidateRecord).where(CandidateRecord.user_id == user_id)))
    by_identity: dict[str, list[CandidateRecord]] = defaultdict(list)
    for c in cands:
        by_identity[c.candidate_identity].append(c)
    dup_groups = {k: v for k, v in by_identity.items() if len(v) > 1}
    return {"total_candidates": len(cands), "duplicate_groups": len(dup_groups), "groups": {k: len(v) for k, v in dup_groups.items()}}
