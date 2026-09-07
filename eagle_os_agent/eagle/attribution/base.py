from dataclasses import dataclass
from typing import Protocol

from eagle.domain.enums import CandidateType, EvidenceDirection, EvidenceStrength, PreferenceHardness
from eagle.domain.scene import Scene


@dataclass(frozen=True)
class AttributionResult:
    candidate_type: CandidateType
    key: str
    value: dict[str, object]
    scene: Scene
    direction: EvidenceDirection
    strength: EvidenceStrength
    contribution: int
    confidence_delta: float
    reason: str
    explicit: bool = False
    user_confirmed: bool = False
    independent_choice_delta: int = 0
    same_condition_success_delta: int = 0
    preference_hardness: PreferenceHardness | None = None


class Attributor(Protocol):
    def attribute(self, episode) -> list[AttributionResult]: ...
