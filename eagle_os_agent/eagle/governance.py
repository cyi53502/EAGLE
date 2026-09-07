import hashlib
import json
from dataclasses import asdict, dataclass

from sqlalchemy import select, text

from eagle.attribution.rule_based import RuleBasedAttributor, explicit_preference_attribution
from eagle.candidate.service import CandidateService
from eagle.db.orm import EpisodeRecord
from eagle.domain.enums import CandidateState, CandidateType, EvidenceDirection
from eagle.episode.collector import EpisodeCollector
from eagle.gate.commitment import CommitmentGate
from eagle.knowledge.service import KnowledgeService
from eagle.preference.service import PreferenceService


@dataclass(frozen=True)
class GovernanceResult:
    episode_id: str
    candidate_ids: tuple[str, ...]
    committed_memory_ids: tuple[str, ...]


class GovernanceService:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.collector = EpisodeCollector()
        self.attributor = RuleBasedAttributor()
        self.candidates = CandidateService()
        self.gate = CommitmentGate()
        self.preferences = PreferenceService()
        self.knowledge = KnowledgeService()

    def record_episode(self, episode_input, explicit_preferences=()) -> GovernanceResult:
        explicit_preferences = tuple(explicit_preferences)
        ingest_fingerprint = self._ingest_fingerprint(episode_input, explicit_preferences)
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = session.scalar(
                select(EpisodeRecord).where(
                    EpisodeRecord.user_id == episode_input.user_id,
                    EpisodeRecord.execution_id == episode_input.execution_id,
                )
            )
            if existing is not None:
                if existing.ingest_fingerprint != "FORGOTTEN" and existing.ingest_fingerprint != ingest_fingerprint:
                    raise ValueError("execution_id was already used with a different Episode payload")
                stored = existing.governance_result_json
                return GovernanceResult(
                    episode_id=stored["episode_id"],
                    candidate_ids=tuple(stored["candidate_ids"]),
                    committed_memory_ids=tuple(stored["committed_memory_ids"]),
                )

            episode = self.collector.collect(session, episode_input, ingest_fingerprint)
            attributions = self.attributor.attribute(episode)
            attributions.extend(explicit_preference_attribution(event) for event in explicit_preferences)

            candidate_ids = []
            committed_ids = []
            for attribution in attributions:
                candidate = self.candidates.apply_evidence(session, episode, attribution)
                candidate_ids.append(candidate.id)
                if attribution.direction is EvidenceDirection.NEGATIVE:
                    if candidate.candidate_type == CandidateType.PREFERENCE.value:
                        self.preferences.revoke_for_candidate(session, candidate)
                    else:
                        self.knowledge.revoke_for_candidate(session, candidate)
                    candidate.state = CandidateState.REJECTED.value
                    continue
                if candidate.state == CandidateState.COMMITTED.value:
                    continue

                self.candidates.mark_preference_conflict(session, candidate)
                self.candidates.mark_knowledge_conflict(session, candidate)
                decision = self.gate.decide(candidate)
                if decision is CandidateState.COMMITTED:
                    if candidate.candidate_type == CandidateType.PREFERENCE.value:
                        memory = self.preferences.commit(session, candidate)
                    else:
                        memory = self.knowledge.commit(session, candidate)
                    committed_ids.append(memory.id)
                else:
                    candidate.state = decision.value

            result = GovernanceResult(
                episode_id=episode.id,
                candidate_ids=tuple(candidate_ids),
                committed_memory_ids=tuple(committed_ids),
            )
            episode.governance_result_json = asdict(result)
            session.commit()
            return result

    @staticmethod
    def _ingest_fingerprint(episode_input, explicit_preferences) -> str:
        payload = {
            "episode": asdict(episode_input),
            "explicit_preferences": [asdict(item) for item in explicit_preferences],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
