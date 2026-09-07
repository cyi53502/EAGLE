from enum import Enum


class CandidateType(str, Enum):
    PREFERENCE = "P"
    KNOWLEDGE = "K"


class CandidateState(str, Enum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    DEFER = "DEFER"


class EvidenceDirection(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class EvidenceStrength(str, Enum):
    STRONG = "STRONG"
    WEAK = "WEAK"


class PreferenceHardness(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"


class PreferenceStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MASKED = "MASKED"
    REVOKED = "REVOKED"
    FORGETTING = "FORGETTING"
    FORGOTTEN = "FORGOTTEN"


class KnowledgeStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
    REVOKED = "REVOKED"
    FORGETTING = "FORGETTING"
    FORGOTTEN = "FORGOTTEN"


class IndexJobOperation(str, Enum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"
    DELETE_DUPLICATE = "DELETE_DUPLICATE"


class IndexJobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
