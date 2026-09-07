from dataclasses import dataclass


@dataclass(frozen=True)
class NoFeasibleAction:
    code: str = "NO_FEASIBLE_ACTION"


NO_FEASIBLE_ACTION = NoFeasibleAction()


@dataclass(frozen=True)
class PlannerConstraint:
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = frozenset()
    require_offline: bool = False
    allowed_formats: frozenset[str] | None = None
    privacy_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannerContext:
    constraints: PlannerConstraint
    knowledge: tuple[dict, ...]
    evidence: tuple[dict, ...] = ()
