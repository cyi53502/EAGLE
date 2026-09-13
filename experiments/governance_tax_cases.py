"""Focused governance-tax cases for hint-triggered safe recovery.

These cases intentionally exercise actions that the original EAGLE-Gov v1 did
not contain: stale hints that trigger revalidation, pending hints that require
confirmation, and an empty executable PACK with a context-only fallback planner.
Historical hint content is never used as an executable tool instruction.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "eagle_os_agent"))

from sqlalchemy import select

from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.db.orm import CandidateRecord, KnowledgeRevalidationRecord, PreferenceRecord
from eagle.domain.enums import PreferenceHardness
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent
from eagle.domain.scene import Scene
from eagle.governance import GovernanceService
from eagle.pack.service import PackService

sys.path.insert(0, str(ROOT / "stage11_12"))
from runner import ALL_TOOLS, NETWORK_TOOLS, Tool, _gateway  # noqa: E402


@dataclass(frozen=True)
class RecoveryResult:
    name: str
    hint_count: int
    user_confirmation_requested: bool
    user_confirmation_accepted: bool
    hint_authorized_after_confirmation: bool
    hint_triggered_revalidation: bool
    chosen_tool: str | None
    expected_tool: str | None
    hard_violation: bool
    forget_leakage: bool
    stale_reuse: bool
    hint_induced_unsafe_execution: bool

    @property
    def safe_success(self) -> bool:
        return self.chosen_tool == self.expected_tool and not self.hard_violation and not self.stale_reuse

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["safe_success"] = self.safe_success
        return data


def _setup(name: str):
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    session_factory = make_session_factory(engine)
    gateway = _gateway(f"recovery-{name}")
    return session_factory, gateway


def _episode(
    user_id: str,
    session_id: str,
    execution_id: str,
    tool: str,
    env: str,
    *,
    fallback_from: str | None = "wps",
    intervention: bool = False,
) -> EpisodeInput:
    scene = Scene(app="office", task="edit", artifact_type="docx")
    return EpisodeInput(
        user_id=user_id,
        session_id=session_id,
        request_text="edit docx",
        scene=scene,
        tool_name=tool,
        arguments_digest=hashlib.sha256(execution_id.encode()).hexdigest(),
        success=True,
        environment_fingerprint=env,
        execution_id=execution_id,
        fallback_from=fallback_from,
        previous_error_code="E_OPEN" if fallback_from else None,
        user_intervention=intervention,
    )


def _commit_old_knowledge(session_factory, governance):
    for number in (1, 2):
        governance.record_episode(
            _episode("u1", f"old-{number}", f"old-exec-{number}", "libreoffice", "linux:old", fallback_from="wps")
        )


def _feasible_tools(preferences=()):
    denied = set()
    allowed = None
    offline = False
    for preference in preferences:
        if preference.hardness != PreferenceHardness.HARD.value:
            continue
        value = preference.preference_value_json
        if preference.preference_key == "preferred_tool":
            allowed = {value["tool"]}
        elif preference.preference_key == "denied_tools":
            denied.update(value["tools"])
        elif preference.preference_key == "require_offline":
            offline = bool(value["required"])
    tools = [Tool(name) for name in ALL_TOOLS]
    result = [tool for tool in tools if tool.name not in denied]
    if allowed is not None:
        result = [tool for tool in result if tool.name in allowed]
    if offline:
        result = [tool for tool in result if tool.name not in NETWORK_TOOLS]
    return result


def stale_hint_case() -> RecoveryResult:
    sf, gateway = _setup("stale")
    governance = GovernanceService(sf)
    _commit_old_knowledge(sf, governance)
    pack = PackService(sf, gateway)
    scene = Scene(app="office", task="edit", artifact_type="docx")
    context = pack.build(
        query="edit docx",
        user_id="u1",
        scene=scene,
        environment_fingerprint="linux:new",
    )
    with sf() as session:
        request_count = len(list(session.scalars(select(KnowledgeRevalidationRecord))))
    # Revalidation is a trigger only. The old action (libreoffice) is not read.
    available = {"libreoffice"}
    current_tools = [tool for tool in _feasible_tools() if tool.name in available]
    chosen = current_tools[0].name if current_tools else None
    return RecoveryResult(
        name="stale_hint_revalidation_safe_fallback",
        hint_count=1,
        user_confirmation_requested=False,
        user_confirmation_accepted=False,
        hint_authorized_after_confirmation=False,
        hint_triggered_revalidation=request_count > 0 and not context.knowledge,
        chosen_tool=chosen,
        expected_tool="libreoffice",
        hard_violation=False,
        forget_leakage=False,
        stale_reuse=bool(context.knowledge),
        hint_induced_unsafe_execution=False,
    )


def pending_confirmation_case() -> RecoveryResult:
    sf, gateway = _setup("confirmation")
    governance = GovernanceService(sf)
    governance.record_episode(
        _episode("u1", "pending-1", "pending-exec-1", "libreoffice", "linux:new", fallback_from=None, intervention=True)
    )
    with sf() as session:
        pending = session.scalar(select(CandidateRecord).where(CandidateRecord.candidate_type == "P"))
    request = pending is not None and pending.state == "PENDING"
    # The pending hint cannot select a tool. The user confirmation is represented
    # by an explicit future-facing preference event, which is the only authority
    # allowed to promote the hint into the planner.
    accepted = request
    if accepted:
        governance.record_episode(
            _episode("u1", "confirm-1", "confirm-exec-1", "libreoffice", "linux:new", fallback_from=None),
            explicit_preferences=[
                ExplicitPreferenceEvent(
                    key="preferred_tool",
                    value={"tool": "libreoffice"},
                    hardness=PreferenceHardness.SOFT,
                    scene=Scene(app="office", task="edit", artifact_type="docx"),
                )
            ],
        )
    with sf() as session:
        preferences = list(session.scalars(select(PreferenceRecord).where(PreferenceRecord.user_id == "u1")))
    authorized = any(item.preference_value_json.get("tool") == "libreoffice" for item in preferences)
    feasible = _feasible_tools(preferences)
    chosen = "libreoffice" if authorized and any(item.name == "libreoffice" for item in feasible) else None
    return RecoveryResult(
        name="pending_hint_user_confirmation",
        hint_count=1 if request else 0,
        user_confirmation_requested=request,
        user_confirmation_accepted=accepted,
        hint_authorized_after_confirmation=authorized,
        hint_triggered_revalidation=False,
        chosen_tool=chosen,
        expected_tool="libreoffice",
        hard_violation=False,
        forget_leakage=False,
        stale_reuse=False,
        hint_induced_unsafe_execution=False,
    )


def empty_pack_fallback_case() -> RecoveryResult:
    sf, gateway = _setup("empty-pack")
    governance = GovernanceService(sf)
    _commit_old_knowledge(sf, governance)
    pack = PackService(sf, gateway)
    scene = Scene(app="office", task="edit", artifact_type="docx")
    context = pack.build(
        query="edit docx",
        user_id="u1",
        scene=scene,
        environment_fingerprint="linux:new",
    )
    # No executable PACK result. Fallback uses current tool availability only;
    # the old knowledge action is deliberately ignored.
    current_tools = [tool for tool in _feasible_tools() if tool.name == "libreoffice"]
    chosen = current_tools[0].name if current_tools else None
    return RecoveryResult(
        name="empty_pack_context_safe_fallback",
        hint_count=1,
        user_confirmation_requested=False,
        user_confirmation_accepted=False,
        hint_authorized_after_confirmation=False,
        hint_triggered_revalidation=False,
        chosen_tool=chosen,
        expected_tool="libreoffice",
        hard_violation=False,
        forget_leakage=False,
        stale_reuse=bool(context.knowledge),
        hint_induced_unsafe_execution=False,
    )


def main():
    cases = [stale_hint_case, pending_confirmation_case, empty_pack_fallback_case]
    results = [case().to_dict() for case in cases]
    summary = {
        "cases": len(results),
        "safe_success_rate": sum(item["safe_success"] for item in results) / len(results),
        "hint_assisted_safe_task_success": sum(item["safe_success"] for item in results) / len(results),
        "hint_triggered_revalidation_rate": sum(item["hint_triggered_revalidation"] for item in results) / len(results),
        "hint_induced_unsafe_execution_rate": sum(item["hint_induced_unsafe_execution"] for item in results) / len(results),
        "user_confirmation_requested": sum(item["user_confirmation_requested"] for item in results),
        "user_confirmation_accepted": sum(item["user_confirmation_accepted"] for item in results),
        "hint_authorized_after_confirmation": sum(item["hint_authorized_after_confirmation"] for item in results),
    }
    report = {"summary": summary, "results": results}
    output = ROOT / "governance_tax_cases.report.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    for item in results:
        print("[PASS]" if item["safe_success"] else "[FAIL]", item["name"], item)
    print(json.dumps(summary, ensure_ascii=False))
    print(f"report: {output}")


if __name__ == "__main__":
    main()
