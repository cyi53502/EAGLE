from collections.abc import Iterable

from eagle.db.orm import PreferenceRecord
from eagle.domain.constraints import NO_FEASIBLE_ACTION, PlannerConstraint
from eagle.domain.enums import PreferenceHardness


class PreferenceCompiler:
    def compile(self, preferences: Iterable[PreferenceRecord]) -> PlannerConstraint:
        allowed_tools = None
        denied_tools = set()
        require_offline = False
        allowed_formats = None
        privacy_rules = []

        for preference in preferences:
            if preference.hardness != PreferenceHardness.HARD.value:
                continue

            value = preference.preference_value_json
            if preference.preference_key == "preferred_tool":
                allowed_tools = frozenset({str(value["tool"])})
            elif preference.preference_key == "denied_tools":
                denied_tools.update(str(tool) for tool in value["tools"])
            elif preference.preference_key == "require_offline":
                require_offline = bool(value["required"])
            elif preference.preference_key in {"allowed_formats", "privacy_rule"}:
                raise ValueError(
                    f"HARD preference key {preference.preference_key} has no executable constraint adapter"
                )
            else:
                raise ValueError(f"Unsupported HARD preference key: {preference.preference_key}")

        return PlannerConstraint(
            allowed_tools=allowed_tools,
            denied_tools=frozenset(denied_tools),
            require_offline=require_offline,
            allowed_formats=allowed_formats,
            privacy_rules=tuple(privacy_rules),
        )


def apply_constraints(tools, constraints: PlannerConstraint):
    original = list(tools)
    selected = [tool for tool in original if tool.name not in constraints.denied_tools]
    if constraints.allowed_tools is not None:
        selected = [tool for tool in selected if tool.name in constraints.allowed_tools]
    if constraints.require_offline:
        selected = [tool for tool in selected if not tool.requires_network]
    tool_constraint_applied = bool(
        constraints.denied_tools or constraints.allowed_tools is not None or constraints.require_offline
    )
    if original and not selected and tool_constraint_applied:
        return NO_FEASIBLE_ACTION
    return selected
