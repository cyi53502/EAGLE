import uuid
from dataclasses import dataclass, field

from eagle.domain.enums import CandidateType, PreferenceHardness
from eagle.domain.scene import Scene


@dataclass(frozen=True)
class EpisodeInput:
    user_id: str
    session_id: str
    request_text: str
    scene: Scene
    tool_name: str
    arguments_digest: str
    success: bool
    environment_fingerprint: str
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    result_class: str | None = None
    error_code: str | None = None
    latency_ms: int = 0
    retry_count: int = 0
    fallback_from: str | None = None
    previous_error_code: str | None = None
    user_intervention: bool = False
    user_correction: "UserCorrectionEvent | None" = None


@dataclass(frozen=True)
class UserCorrectionEvent:
    candidate_type: CandidateType
    key: str
    value: dict[str, object]
    scene: Scene = field(default_factory=Scene)


@dataclass(frozen=True)
class ExplicitPreferenceEvent:
    key: str
    value: dict[str, object]
    hardness: PreferenceHardness
    scene: Scene = field(default_factory=Scene)
