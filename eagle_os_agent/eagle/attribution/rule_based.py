from eagle.attribution.base import AttributionResult
from eagle.domain.enums import CandidateType, EvidenceDirection, EvidenceStrength, PreferenceHardness
from eagle.domain.events import ExplicitPreferenceEvent
from eagle.domain.scene import Scene


class RuleBasedAttributor:
    def attribute(self, episode) -> list[AttributionResult]:
        results = []
        if episode.user_correction_json:
            results.append(self._correction(episode))
        if episode.fallback_from and episode.success:
            results.append(self._fallback(episode))
        elif episode.user_intervention and episode.success:
            results.append(self._independent_tool_choice(episode))
        return results

    @staticmethod
    def _correction(episode) -> AttributionResult:
        correction = episode.user_correction_json
        return AttributionResult(
            candidate_type=CandidateType(correction["candidate_type"]),
            key=correction["key"],
            value=correction["value"],
            scene=Scene.from_dict(correction["scene"]),
            direction=EvidenceDirection.NEGATIVE,
            strength=EvidenceStrength.STRONG,
            contribution=1,
            confidence_delta=-1.0,
            reason="explicit user correction",
        )

    @staticmethod
    def _fallback(episode) -> AttributionResult:
        return AttributionResult(
            candidate_type=CandidateType.KNOWLEDGE,
            key=(f"fallback:{episode.fallback_from}:{episode.previous_error_code}:{episode.tool_name}"),
            value={
                "type": "WORKFLOW",
                "when": {
                    "tool": episode.fallback_from,
                    "error_code": episode.previous_error_code,
                    "environment": episode.environment_fingerprint,
                },
                "action": {"fallback_to": episode.tool_name},
            },
            scene=Scene.from_dict(episode.scene_json),
            direction=EvidenceDirection.POSITIVE,
            strength=EvidenceStrength.WEAK,
            contribution=1,
            confidence_delta=0.35,
            same_condition_success_delta=1,
            reason="primary tool failed and fallback succeeded",
        )

    @staticmethod
    def _independent_tool_choice(episode) -> AttributionResult:
        return AttributionResult(
            candidate_type=CandidateType.PREFERENCE,
            key="preferred_tool",
            value={"tool": episode.tool_name},
            scene=Scene.from_dict(episode.scene_json),
            direction=EvidenceDirection.POSITIVE,
            strength=EvidenceStrength.WEAK,
            contribution=1,
            confidence_delta=0.2,
            independent_choice_delta=1,
            preference_hardness=PreferenceHardness.SOFT,
            reason="user independently selected a successful tool",
        )


def explicit_preference_attribution(event: ExplicitPreferenceEvent) -> AttributionResult:
    return AttributionResult(
        candidate_type=CandidateType.PREFERENCE,
        key=event.key,
        value=event.value,
        scene=event.scene,
        direction=EvidenceDirection.POSITIVE,
        strength=EvidenceStrength.STRONG,
        contribution=1,
        confidence_delta=1.0,
        explicit=True,
        preference_hardness=event.hardness,
        reason="explicit future-facing user preference",
    )
