"""EAGLE-Gov runner: executes each dataset case against a live local EAGLE stack.

No Kylin SDK and no Mem0 vectors are required: PACK recall uses an in-memory
SearchGateway that serves exactly the eligible authoritative rows, which keeps
the governance assertions (attribution / gate / constraints / visibility) fully
verifiable before the real Kylin smoke stage. The Mem0 provider path itself is
covered by stages 6-8.

Scoring: every expected key is checked against observed behavior; a case passes
only when all checks pass. Aggregate metrics follow EAGLE-experiment.md §11.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from eagle.conflict.service import ConflictService
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.db.orm import (
    CandidateRecord,
    EpisodeRecord,
    EvidenceRecord,
    IndexJobRecord,
    KnowledgeRecord,
    PreferenceRecord,
)
from eagle.domain.constraints import NO_FEASIBLE_ACTION
from eagle.domain.enums import PreferenceHardness
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent, UserCorrectionEvent
from eagle.domain.scene import Scene
from eagle.forgetting.service import ForgettingService
from eagle.governance import GovernanceService
from eagle.pack.service import PackService
from eagle.preference.compiler import apply_constraints

_generate_spec = importlib.util.spec_from_file_location("eagle_gov_generate", Path(__file__).parent / "generate.py")
_generate = importlib.util.module_from_spec(_generate_spec)
sys.modules["eagle_gov_generate"] = _generate
_generate_spec.loader.exec_module(_generate)

APPS = ["office", "browser", "terminal", "files"]
TOOLS = {
    "office": ["wps", "libreoffice", "onlyoffice"],
    "browser": ["firefox", "chromium", "wget"],
    "terminal": ["apt", "dnf", "pacman"],
    "files": ["mc", "ranger", "dolphin"],
}
ALL_TOOLS = sorted({tool for tools in TOOLS.values() for tool in tools})
NETWORK_TOOLS = {"wget", "apt", "dnf", "pacman", "firefox", "chromium"}


class Tool:
    def __init__(self, name):
        self.name = name
        self.requires_network = name in NETWORK_TOOLS


class SearchGateway:
    """Serves PACK from the authoritative SQLite rows (stage 9 pre-Kylin mode)."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def search_knowledge(self, *, query, user_id, limit, eligible_memory_ids):
        with self.session_factory() as session:
            rows = list(
                session.scalars(
                    select(KnowledgeRecord).where(
                        KnowledgeRecord.user_id == user_id,
                        KnowledgeRecord.status == "ACTIVE",
                    )
                )
            )
        results = []
        for knowledge in rows:
            if knowledge.mem0_id and knowledge.mem0_id in eligible_memory_ids:
                results.append({"id": knowledge.mem0_id, "score": 0.9, "metadata": {"eagle_memory_id": knowledge.id}})
        return results[:limit]


@dataclass
class CaseResult:
    case_id: str
    family: str
    user_id: str
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and all(self.checks.values())

    def to_json(self) -> dict:
        return {
            "id": self.case_id,
            "family": self.family,
            "user_id": self.user_id,
            "checks": self.checks,
            "errors": self.errors,
            "passed": self.passed,
        }


def _explicit_events(specs: list[dict]) -> list[ExplicitPreferenceEvent]:
    return [
        ExplicitPreferenceEvent(
            key=spec["key"],
            value=spec["value"],
            hardness=PreferenceHardness(spec["hardness"]),
            scene=Scene(**spec.get("scene", {})),
        )
        for spec in specs
    ]


def run_case(case: dict, session_factory) -> CaseResult:
    result = CaseResult(case_id=case["id"], family=case["family"], user_id=case["user_id"])
    expected = case["expected"]
    governance = GovernanceService(session_factory)
    user_id = case["user_id"]
    committed_ids: list[str] = []

    try:
        for position, raw in enumerate(case["episodes"], start=1):
            correction = raw.get("user_correction")
            episode_input = EpisodeInput(
                user_id=user_id,
                session_id=raw["session_id"],
                request_text=raw["request_text"],
                scene=Scene(**raw["scene"]),
                tool_name=raw["tool_name"],
                arguments_digest=raw["arguments_digest"],
                success=raw["success"],
                environment_fingerprint=raw["environment_fingerprint"],
                execution_id=raw.get("execution_id", f"{case['id']}-exec-{position}"),
                fallback_from=raw.get("fallback_from"),
                previous_error_code=raw.get("previous_error_code"),
                user_intervention=raw.get("user_intervention", False),
                user_correction=(
                    UserCorrectionEvent(
                        candidate_type=correction["candidate_type"],
                        key=correction["key"],
                        value=correction["value"],
                        scene=Scene(**correction.get("scene", {})),
                    )
                    if correction
                    else None
                ),
            )
            outcome = governance.record_episode(
                episode_input,
                explicit_preferences=_explicit_events(raw.get("explicit_preferences", [])),
            )
            committed_ids.extend(outcome.committed_memory_ids)

        with session_factory() as session:
            candidates = list(session.scalars(select(CandidateRecord)))
            knowledge_items = list(session.scalars(select(KnowledgeRecord)))
            preferences = list(session.scalars(select(PreferenceRecord)))
            episodes = list(session.scalars(select(EpisodeRecord)))
            evidence = list(session.scalars(select(EvidenceRecord)))
            states = [candidate.state for candidate in candidates]
    except Exception as error:  # noqa: BLE001 - dataset runner boundary
        result.errors.append(f"execution_failed: {type(error).__name__}: {error}")
        return result

    def scene_of(raw: dict) -> Scene:
        return Scene(**raw["scene"])

    def home_env(case: dict) -> str:
        return case["episodes"][0]["environment_fingerprint"]

    # --- shared checks -----------------------------------------------------
    if "committed_count" in expected:
        result.checks["committed_count"] = len(committed_ids) == expected["committed_count"]
    if "preference_created" in expected:
        result.checks["preference_created"] = bool(preferences) == expected["preference_created"]
    if "knowledge_created" in expected:
        result.checks["knowledge_created"] = bool(knowledge_items) == expected["knowledge_created"]
    if "episodes_stored" in expected:
        result.checks["episodes_stored"] = len(episodes) == expected["episodes_stored"]
    if "evidence_count" in expected:
        result.checks["evidence_count"] = len(evidence) == expected["evidence_count"]
    if "authorization_state" in expected:
        result.checks["authorization_state"] = (
            bool(preferences) and preferences[0].authorization_state == expected["authorization_state"]
        )
    if "hardness" in expected:
        result.checks["hardness"] = bool(preferences) and preferences[0].hardness == expected["hardness"]
    if "negative_evidence" in expected:
        negatives = [candidate.negative_evidence for candidate in candidates]
        result.checks["negative_evidence"] = bool(negatives) and max(negatives) == expected["negative_evidence"]
    if "candidate_state" in expected:
        result.checks["candidate_state"] = expected["candidate_state"] in states
    if "gate_state" in expected:
        result.checks["gate_state"] = expected["gate_state"] in states
    if "preference_status" in expected:
        result.checks["preference_status"] = (
            bool(preferences) and preferences[0].status == expected["preference_status"]
        )
    if "candidate_type" in expected:
        result.checks["candidate_type"] = bool(candidates) and all(
            candidate.candidate_type == expected["candidate_type"] for candidate in candidates
        )

    # --- family-specific checks -------------------------------------------
    if case["family"] == "fallback_not_preference":
        result.checks["no_preference_candidate"] = all(candidate.candidate_type == "K" for candidate in candidates)

    if case["family"] == "kk_conflict":
        result.checks["old_knowledge_status"] = (
            bool(knowledge_items) and knowledge_items[0].status == expected["old_knowledge_status"]
        )
        result.checks["conflicting_candidate_state"] = expected["conflicting_candidate_state"] in states

    if case["family"] == "env_drift" and knowledge_items:
        knowledge = knowledge_items[0]
        # Stage-9 pre-Kylin mode: bind the authoritative row to a stable vector id
        # so the in-memory search gateway can serve PACK recall.
        with session_factory.begin() as session:
            session.get(KnowledgeRecord, knowledge.id).mem0_id = f"govvec-{knowledge.id}"
        knowledge.mem0_id = f"govvec-{knowledge.id}"
        pack_home = PackService(session_factory, SearchGateway(session_factory)).build(
            query="fallback",
            user_id=user_id,
            scene=scene_of(case["episodes"][0]),
            environment_fingerprint=expected["home_environment"],
        )
        pack_drift = PackService(session_factory, SearchGateway(session_factory)).build(
            query="fallback",
            user_id=user_id,
            scene=scene_of(case["episodes"][0]),
            environment_fingerprint=expected["drift_environment"],
        )
        result.checks["knowledge_visible_home_env"] = len(pack_home.knowledge) == (
            1 if expected["knowledge_visible_home_env"] else 0
        )
        result.checks["knowledge_visible_drift_env"] = len(pack_drift.knowledge) == (
            1 if expected["knowledge_visible_drift_env"] else 0
        )
        result.checks["knowledge_active_after_drift"] = (knowledge.status == "ACTIVE") == expected[
            "knowledge_active_after_drift"
        ]

    if case["family"] == "pk_visibility" and knowledge_items:
        knowledge = knowledge_items[0]
        with session_factory.begin() as session:
            session.get(KnowledgeRecord, knowledge.id).mem0_id = f"govvec-{knowledge.id}"
        knowledge.mem0_id = f"govvec-{knowledge.id}"
        # The P-K conflict layer (ConflictService) establishes the reversible
        # visibility mask when a HARD preference forbids what knowledge advises.
        with session_factory.begin() as session:
            preference = session.scalar(select(PreferenceRecord).where(PreferenceRecord.user_id == user_id))
            ConflictService().mask_knowledge(
                session,
                preference_id=preference.id,
                knowledge_id=knowledge.id,
                reason="knowledge requires network while HARD preference requires offline",
            )
        pack = PackService(session_factory, SearchGateway(session_factory)).build(
            query="fallback",
            user_id=user_id,
            scene=scene_of(case["episodes"][0]),
            environment_fingerprint=home_env(case),
        )
        result.checks["knowledge_visible_after_mask"] = len(pack.knowledge) == (
            1 if expected["knowledge_visible_after_mask"] else 0
        )
        result.checks["knowledge_status"] = knowledge.status == expected["knowledge_status"]

    if case["family"] == "hard_constraint":
        if expected["raises"]:
            try:
                PackService(session_factory, SearchGateway(session_factory)).build(
                    query="edit",
                    user_id=user_id,
                    scene=Scene(),
                    environment_fingerprint="linux:probe-1",
                )
            except ValueError as error:
                result.checks["unenforceable_rejected"] = "no executable constraint adapter" in str(error)
            else:
                result.checks["unenforceable_rejected"] = False
        else:
            context = PackService(session_factory, SearchGateway(session_factory)).build(
                query="edit",
                user_id=user_id,
                scene=Scene(),
                environment_fingerprint="linux:probe-1",
            )
            tools = [Tool(name) for name in ALL_TOOLS]
            filtered = apply_constraints(tools, context.constraints)
            if expected["no_feasible"]:
                result.checks["no_feasible_action"] = filtered is NO_FEASIBLE_ACTION
            else:
                result.checks["allowed_tools"] = (
                    filtered is not NO_FEASIBLE_ACTION
                    and sorted(tool.name for tool in filtered) == expected["allowed_tools"]
                )

    if case["family"] == "forgetting" and knowledge_items:
        knowledge = knowledge_items[0]
        with session_factory.begin() as session:
            session.get(KnowledgeRecord, knowledge.id).mem0_id = f"govvec-{knowledge.id}"
        knowledge.mem0_id = f"govvec-{knowledge.id}"
        scene = scene_of(case["episodes"][0])
        env = home_env(case)
        pack_before = PackService(session_factory, SearchGateway(session_factory)).build(
            query="fallback",
            user_id=user_id,
            scene=scene,
            environment_fingerprint=env,
        )
        result.checks["visible_before_forget"] = len(pack_before.knowledge) == (
            1 if expected["visible_before_forget"] else 0
        )
        try:
            ForgettingService(session_factory).forget_knowledge(knowledge.id, user_id="another-user")
            result.checks["cross_user_forget_rejected"] = not expected["cross_user_forget_rejected"]
        except ValueError:
            result.checks["cross_user_forget_rejected"] = expected["cross_user_forget_rejected"]
        ForgettingService(session_factory).forget_knowledge(knowledge.id, user_id=user_id)
        pack_after = PackService(session_factory, SearchGateway(session_factory)).build(
            query="fallback",
            user_id=user_id,
            scene=scene,
            environment_fingerprint=env,
        )
        result.checks["visible_after_forget"] = len(pack_after.knowledge) == (
            1 if expected["visible_after_forget"] else 0
        )

    if case["family"] == "replay_idempotency":
        with session_factory() as session:
            job = session.scalar(select(IndexJobRecord))
            result.checks["no_pending_job"] = job is None

    return result


def aggregate(results: list[CaseResult]) -> dict:
    total = len(results)
    passed = sum(1 for item in results if item.passed)
    by_family: dict[str, dict[str, int]] = {}
    for item in results:
        family_stats = by_family.setdefault(item.family, {"total": 0, "passed": 0})
        family_stats["total"] += 1
        family_stats["passed"] += 1 if item.passed else 0
    return {
        "cases_total": total,
        "cases_passed": passed,
        "case_pass_rate": round(passed / total, 4) if total else 0.0,
        "by_family": by_family,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "v1.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--report", type=Path, default=Path(__file__).parent / "v1.report.json")
    args = parser.parse_args()

    random.seed(args.seed)
    cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = []
    for case in cases:
        engine = create_sqlite_engine(":memory:")
        create_schema(engine)
        session_factory = make_session_factory(engine)
        results.append(run_case(case, session_factory))

    report = {
        "dataset": str(args.dataset),
        "seed": args.seed,
        "aggregate": aggregate(results),
        "failed_cases": [item.to_json() for item in results if not item.passed],
    }
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["aggregate"], indent=2, ensure_ascii=False))
    if report["failed_cases"]:
        print(f"failed cases: {len(report['failed_cases'])} (see {args.report})")


if __name__ == "__main__":
    main()
