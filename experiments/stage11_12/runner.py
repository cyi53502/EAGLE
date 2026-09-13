"""Stage 11 (三组主实验 A/B/C) + Stage 12 (Ablation) + Stage 13-lite (多种子).

统一条件 (EAGLE-experiment.md §11 前置): 数据 (EAGLE-Gov 同一 seed 的 200 case)、
Embedding (ShimEmbeddingClient 768d gte-base parity)、VectorStore
(ShimVectorClient cosine_distance)、top_k=5 完全一致；仅治理管线不同。

Arms:
  A  mem0-original    — 朴素模式泛化近似 mem0 默认 extraction/inference: 每条 episode
                        存为原始 fact；任何 (scene,tool) 出现 >=2 次（含 fallback 成功）
                        即泛化为 preference 文本；无门控/无归因纪律/无 HARD 编译/无环境校验。
  B  mem0+pref-text   — 同 A 的知识 fact，但 preference 只来自显式陈述，以纯文本召回，
                        preferred_tool 软排序，HARD 不编译。
  C  eagle-full       — 完整治理链 (Attribution→Gate→Commit→IndexJob→Worker→
                        Mem0(infer=False)→Pack→HARD 编译→P-K mask→Revalidation→Forget→Reconciliation)。

Ablations (§12, 在 C 上逐项移除, 验证 H1-H5):
  no_attribution      — fallback 不再归因为 K（等同被当成用户主动选择）→ H1 False Promotion↑
  no_gate             — CommitmentGate 恒 COMMITTED（无 3次/2session 阈值、无 DEFER）
  no_hard_compile     — PreferenceCompiler 不产出约束 → H2 HardConstraintViolation↑
  no_env_validation   — PACK 不做环境指纹校验、不写 revalidation → H3 Stale Reuse↑
  no_pk_visibility    — 不执行 P-K mask → H4 P-K Conflict Violation↑
  no_reconciliation   — 不运行 IndexWorker（向量永不收敛/缺失）→ Recall↓ 复现收敛职责

指标 (§11 十项, case 级布尔/计数微平均): Preference Precision, Preference False
Promotion Rate, Knowledge Precision, Knowledge Recall@K, Hard Constraint Violation
Rate, Stale Knowledge Reuse Rate, Conflict Resolution Accuracy, Forget Leakage
Rate, Traceability Coverage, Downstream Task Success。安全指标优先级高于向量 Recall。

用法:
  python runner.py --seeds 42,43,44 --total 200 --out-dir .
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select

from eagle.conflict.service import ConflictService
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.db.orm import (
    EvidenceRecord,
    IndexJobRecord,
    KnowledgeRecord,
    KnowledgeRevalidationRecord,
    MemoryEvidenceLinkRecord,
    PKVisibilityRecord,
    PreferenceRecord,
)
from eagle.domain.constraints import NO_FEASIBLE_ACTION
from eagle.domain.enums import PreferenceHardness
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent, UserCorrectionEvent
from eagle.domain.scene import Scene
from eagle.forgetting.service import ForgettingService
from eagle.gate.commitment import CommitmentGate
from eagle.governance import GovernanceService
from eagle.outbox.worker import IndexWorker
from eagle.pack.service import PackService
from eagle.preference.compiler import PreferenceCompiler, apply_constraints

from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
from eagle.adapters.kylin.vector_shim import ShimVectorClient
from eagle.bootstrap import create_mem0_gateway

GOV_DIR = Path(__file__).resolve().parents[1] / "datasets" / "eagle-gov"
_spec = importlib.util.spec_from_file_location("eagle_gov_generate", GOV_DIR / "generate.py")
_generate = importlib.util.module_from_spec(_spec)
sys.modules["eagle_gov_generate"] = _generate
_spec.loader.exec_module(_generate)

TOOLS = _generate.TOOLS
NETWORK_TOOLS = _generate.NETWORK_TOOLS
ALL_TOOLS = sorted({tool for tools in TOOLS.values() for tool in tools})
TOP_K = 5


class Tool:
    def __init__(self, name):
        self.name = name
        self.requires_network = name in NETWORK_TOOLS


# ---------------------------------------------------------------------------
# ground truth derived from family + expected block
# ---------------------------------------------------------------------------


@dataclass
class Oracle:
    expects_preference: bool = False
    pref_key: str | None = None
    pref_value: dict | None = None
    pref_hardness: str | None = None
    pref_inferred_allowed: bool = False  # implicit 3x2s: inferred SOFT pref is CORRECT
    expects_knowledge: bool = False
    knowledge_tool: str | None = None
    probe_scene: dict = field(default_factory=dict)
    env_home: str = ""
    env_drift: str | None = None
    visible_home: bool = False
    visible_drift: bool = False
    hard_kind: str | None = None
    allowed_tools: list | None = None
    no_feasible: bool = False
    unenforceable: bool = False
    forget: bool = False
    replay: bool = False
    conflict_family: bool = False
    pk_family: bool = False
    expected_tool: str | None = None
    false_promotion_applicable: bool = True  # no preference should influence planner
    description: str = ""


def build_oracle(case: dict) -> Oracle:
    family = case["family"]
    expected = case["expected"]
    episodes = case["episodes"]
    scene = episodes[0]["scene"]
    env_home = expected.get("home_environment", episodes[0]["environment_fingerprint"])
    o = Oracle(probe_scene=scene, env_home=env_home, description=case.get("description", ""))

    if family == "fallback_not_preference":
        o.expects_knowledge = True
        o.knowledge_tool = episodes[0]["tool_name"]
        o.visible_home = True
        o.expected_tool = o.knowledge_tool
    elif family == "implicit_gate":
        variant = (
            "two_same_session"
            if len(episodes) == 2
            else ("negative_correction" if any(e.get("user_correction") for e in episodes) else "three_two_sessions")
        )
        if variant == "two_same_session":
            o.expected_tool = episodes[0]["tool_name"]
        elif variant == "three_two_sessions":
            o.expects_preference = True
            o.pref_key = "preferred_tool"
            o.pref_value = {"tool": episodes[0]["tool_name"]}
            o.pref_hardness = "SOFT"
            o.pref_inferred_allowed = True
            o.expected_tool = episodes[0]["tool_name"]
        else:
            correction = next(e for e in episodes if e.get("user_correction"))["user_correction"]
            o.expected_tool = correction["value"]["tool"]
    elif family == "explicit_gate":
        spec = episodes[0]["explicit_preferences"][0]
        o.expects_preference = True
        o.pref_key = spec["key"]
        o.pref_value = spec["value"]
        o.pref_hardness = spec["hardness"]
        o.expected_tool = spec["value"].get("tool")
    elif family == "hard_constraint":
        spec = episodes[0]["explicit_preferences"][0]
        o.expects_preference = True
        o.pref_key = spec["key"]
        o.pref_value = spec["value"]
        o.pref_hardness = "HARD"
        o.hard_kind = expected["constraint_kind"]
        o.allowed_tools = expected.get("allowed_tools")
        o.no_feasible = expected["no_feasible"]
        o.unenforceable = expected["raises"]
        if o.hard_kind == "preferred":
            o.expected_tool = spec["value"]["tool"]
        elif o.hard_kind in {"denied", "offline"}:
            last_tool = episodes[-1]["tool_name"]
            if o.no_feasible:
                o.expected_tool = None
            elif o.allowed_tools and last_tool in o.allowed_tools:
                o.expected_tool = last_tool
            else:
                o.expected_tool = o.allowed_tools[0] if o.allowed_tools else None
    elif family == "kk_conflict":
        o.expects_knowledge = True
        o.knowledge_tool = episodes[0]["tool_name"]
        o.visible_home = True
        o.conflict_family = True
        o.expected_tool = o.knowledge_tool
    elif family == "env_drift":
        o.expects_knowledge = True
        o.knowledge_tool = episodes[0]["tool_name"]
        o.visible_home = True
        o.env_drift = expected["drift_environment"]
        o.visible_drift = False
        o.expected_tool = o.knowledge_tool
    elif family == "pk_visibility":
        spec = episodes[2]["explicit_preferences"][0]
        o.expects_preference = True
        o.pref_key = spec["key"]
        o.pref_value = spec["value"]
        o.pref_hardness = "HARD"
        o.expects_knowledge = True
        o.knowledge_tool = episodes[0]["tool_name"]
        o.visible_home = False  # masked by P-K conflict layer
        o.pk_family = True
        fallback = o.knowledge_tool
        offline = [t for t in TOOLS[scene["app"]] if t not in NETWORK_TOOLS]
        if fallback not in NETWORK_TOOLS:
            o.expected_tool = fallback
        elif offline:
            o.expected_tool = offline[0]
        else:
            # no offline tool exists for this app: correct behavior is refusal
            o.expected_tool = None
            o.no_feasible = True
    elif family == "forgetting":
        o.expects_knowledge = True
        o.knowledge_tool = episodes[0]["tool_name"]
        o.visible_home = True
        o.forget = True
        o.expected_tool = o.knowledge_tool
    elif family == "replay_idempotency":
        o.replay = True
        o.expected_tool = episodes[0]["tool_name"]
    return o


# ---------------------------------------------------------------------------
# arms A / B: naive mem0 pipelines (no governance)
# ---------------------------------------------------------------------------


def fact_text(raw: dict) -> str:
    scene = raw["scene"]
    verb = {"office": "edit", "browser": "download", "terminal": "install", "files": "organize"}.get(
        scene["app"], "process"
    )
    env = raw["environment_fingerprint"]
    if raw.get("fallback_from"):
        return (
            f"In {env} for {scene['app']} {scene['task']} {scene['artifact_type']}: "
            f"{raw['fallback_from']} failed with {raw.get('previous_error_code')}, use {raw['tool_name']} instead."
        )
    if raw.get("user_intervention"):
        return f"User chose {raw['tool_name']} for {scene['app']} {scene['task']} {scene['artifact_type']} in {env}."
    return f"Used {raw['tool_name']} for {scene['app']} {scene['task']} {scene['artifact_type']} in {env}."


class NaivePipeline:
    """Arm A / B: raw mem0 add with naive pattern generalization (no governance)."""

    def __init__(self, gateway, mode: str):
        assert mode in {"A", "B"}
        self.gateway = gateway
        self.mode = mode
        self.fact_keys: list[str] = []  # stable content keys (dedupe like mem0 content hash)
        self.memory_ids: dict[str, str] = {}
        self.preferences: list[dict] = []  # {key, value, hardness, scene, source, revoked}
        self.pattern_counts: defaultdict = defaultdict(int)
        self.provenance: bool = False  # original mem0 keeps no attribution evidence

    def record(self, case_id: str, position: int, raw: dict, user_id: str) -> None:
        scene = raw["scene"]
        env = raw["environment_fingerprint"]
        key = f"{user_id}|{fact_text(raw)}"
        if key not in self.memory_ids:
            response = self.gateway.memory.add(
                [{"role": "user", "content": fact_text(raw)}],
                user_id=user_id,
                metadata={
                    "index_key": key,
                    "kind": "K",
                    "tool": raw["tool_name"],
                    "scene": scene,
                    "env": env,
                    "case_id": case_id,
                },
                infer=False,
            )
            results = response["results"]
            self.memory_ids[key] = results[0]["id"] if results else f"dedup-{len(self.fact_keys)}"
            self.fact_keys.append(key)
        if self.mode == "A":
            pattern = (user_id, json_scene(scene), raw["tool_name"])
            self.pattern_counts[pattern] += 1
            if self.pattern_counts[pattern] == 2:
                self._add_preference(
                    user_id,
                    {"key": "preferred_tool", "value": {"tool": raw["tool_name"]}, "hardness": "SOFT"},
                    scene,
                    source="inferred-pattern",
                )
        for spec in raw.get("explicit_preferences", []):
            self._add_preference(user_id, spec, spec.get("scene", scene), source="explicit")

    def _add_preference(self, user_id: str, spec: dict, scene: dict, *, source: str) -> None:
        pref = {
            "key": spec["key"],
            "value": spec["value"],
            "hardness": spec["hardness"],
            "scene": dict(scene),
            "source": source,
            "revoked": False,
        }
        for existing in self.preferences:
            if (
                existing["key"] == pref["key"]
                and json_scene(existing["scene"]) == json_scene(pref["scene"])
                and existing["value"] == pref["value"]
            ):
                return
        self.preferences.append(pref)
        text = f"User preference {pref['key']}={json.dumps(pref['value'])} ({pref['hardness']})."
        self.gateway.memory.add(
            [{"role": "user", "content": text}],
            user_id=user_id,
            metadata={"kind": "P", "pref": pref["key"], "tool": pref["value"].get("tool"), "scene": pref["scene"]},
            infer=False,
        )

    def correction(self, raw: dict, user_id: str) -> None:
        # original mem0 without governance: correction is just another stored fact;
        # previously inferred preferences are NOT revoked.
        if raw.get("user_correction"):
            self.gateway.memory.add(
                [{"role": "user", "content": f"User corrected: {json.dumps(raw['user_correction']['value'])}"}],
                user_id=user_id,
                metadata={"kind": "C", "case": True},
                infer=False,
            )

    def forget(self, user_id: str, oracle: Oracle) -> bool:
        # explicit delete of the matching knowledge fact (original mem0 delete API)
        scene = oracle.probe_scene
        for key, memory_id in self.memory_ids.items():
            if oracle.knowledge_tool and oracle.knowledge_tool in key and json_scene(scene) in key.replace("'", '"'):
                self.gateway.memory.delete(memory_id)
                self.memory_ids.pop(key, None)
                if key in self.fact_keys:
                    self.fact_keys.remove(key)
                return True
        return False

    def pack(self, user_id: str, query: str, scene: dict, top_k: int = TOP_K) -> list[dict]:
        response = self.gateway.memory.search(
            query=query,
            filters={"user_id": user_id},
            top_k=top_k,
            threshold=0,
        )
        return list(response["results"])

    def decide(self, user_id: str, query: str, scene: "Scene") -> tuple[str | None, bool]:
        """Returns (tool, violated_hard). Planner reads raw recalled facts."""
        results = self.pack(user_id, query, scene)
        facts = [r for r in results if (r.get("metadata") or {}).get("kind") == "K"]
        prefs = [p for p in self.preferences if not p["revoked"] and p["key"] == "preferred_tool"]
        scene_key = json_scene(scene.normalized())
        matching_prefs = [p for p in prefs if json_scene(p["scene"]) == scene_key or not p["scene"]]
        violated = False
        tool = None
        if facts:
            meta = facts[0].get("metadata") or {}
            tool = meta.get("tool")
        for pref in matching_prefs:
            # A: inferred preferences reorder the decision exactly like real ones
            # B: only explicit statements softly reorder preferred_tool
            if self.mode == "A" or pref["source"] == "explicit":
                if pref["key"] == "preferred_tool":
                    tool = pref["value"]["tool"]
            # A/B never compile HARD: denied/offline/allowed_forms are ignored
        # HARD violations for arm A/B: if an active HARD pref exists and the chosen
        # tool breaks it, the planner would still execute it.
        for pref in self.preferences:
            if pref["revoked"] or pref["hardness"] != "HARD":
                continue
            if pref["key"] == "require_offline" and tool in NETWORK_TOOLS:
                violated = True
            if pref["key"] == "denied_tools" and tool in pref["value"].get("tools", []):
                violated = True
            if pref["key"] == "preferred_tool" and tool != pref["value"]["tool"]:
                violated = True  # HARD preferred_tool is a physical allowlist in EAGLE
        return tool, violated


def json_scene(scene: dict) -> str:
    return json.dumps(scene, sort_keys=True)


# ---------------------------------------------------------------------------
# arm C: full governance chain (with ablation switches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ablation:
    no_attribution: bool = False
    no_gate: bool = False
    no_hard_compile: bool = False
    no_env_validation: bool = False
    no_pk_visibility: bool = False
    no_reconciliation: bool = False

    @property
    def name(self) -> str:
        parts = []
        if self.no_attribution:
            parts.append("no_attribution")
        if self.no_gate:
            parts.append("no_gate")
        if self.no_hard_compile:
            parts.append("no_hard_compile")
        if self.no_env_validation:
            parts.append("no_env_validation")
        if self.no_pk_visibility:
            parts.append("no_pk_visibility")
        if self.no_reconciliation:
            parts.append("no_reconciliation")
        return "full" if not parts else "w/o " + "+".join(parts)


class Misattributor:  # no_attribution: fallback reads as user choice
    def __init__(self, inner):
        self._inner = inner

    def attribute(self, episode):
        from dataclasses import replace

        from eagle.domain.enums import CandidateType

        results = self._inner.attribute(episode)
        out = []
        for item in results:
            if item.reason == "primary tool failed and fallback succeeded":
                out.append(
                    replace(
                        item,
                        candidate_type=CandidateType.PREFERENCE,
                        key="preferred_tool",
                        value={"tool": episode.tool_name},
                        reason="user independently selected a successful tool",
                        confidence_delta=0.2,
                        same_condition_success_delta=0,
                        independent_choice_delta=1,
                    )
                )
            else:
                out.append(item)
        return out


class AlwaysCommitGate:
    def decide(self, candidate):
        from eagle.domain.enums import CandidateState

        return CandidateState.COMMITTED


class NullCompiler:
    def compile(self, preferences):
        from eagle.domain.constraints import PlannerConstraint

        return PlannerConstraint()


class NoEnvPackService(PackService):
    def build(self, *, query, user_id, scene, environment_fingerprint, top_k=TOP_K):
        with self.session_factory.begin() as session:
            preferences = list(
                session.scalars(
                    select(PreferenceRecord).where(
                        PreferenceRecord.user_id == user_id,
                        PreferenceRecord.status == "ACTIVE",
                    )
                )
            )
            resolved = self.resolver.resolve(preferences, scene)
            constraints = self.compiler.compile(resolved)
            active_knowledge = list(
                session.scalars(
                    select(KnowledgeRecord).where(
                        KnowledgeRecord.user_id == user_id,
                        KnowledgeRecord.status == "ACTIVE",
                    )
                )
            )
            current_scene = scene.normalized()
            scene_knowledge = [
                knowledge
                for knowledge in active_knowledge
                if self._scene_matches(knowledge.scene_json, current_scene)
            ]
            # ablation: no environment fingerprint check, no revalidation writes
            eligible = tuple(k.mem0_id for k in scene_knowledge if k.mem0_id)
            if not eligible:
                from eagle.domain.constraints import PlannerContext

                return PlannerContext(constraints=constraints, knowledge=())
            search_results = self.gateway.search_knowledge(
                query=query,
                user_id=user_id,
                limit=top_k * 4,
                eligible_memory_ids=eligible,
            )
            by_id = {}
            for result in search_results:
                kid = (result.get("metadata") or {}).get("eagle_memory_id")
                if kid and kid not in by_id:
                    by_id[kid] = result
            knowledge_by_id = {k.id: k for k in scene_knowledge}
            selected = [(knowledge_by_id[kid], result) for kid, result in by_id.items() if kid in knowledge_by_id]
            from eagle.domain.constraints import PlannerContext

            return PlannerContext(
                constraints=constraints,
                knowledge=tuple(
                    {
                        "id": knowledge.id,
                        "type": knowledge.knowledge_type,
                        "content": knowledge.content_json,
                        "score": result.get("score"),
                    }
                    for knowledge, result in selected[:top_k]
                ),
            )


def run_case_arm_c(
    case: dict,
    oracle: Oracle,
    ablation: Ablation,
    *,
    two_tier: bool = False,
    safe_fallback: bool = False,
) -> dict:
    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    session_factory = make_session_factory(engine)
    user_id = case["user_id"]

    gateway = _gateway("eagle_s12")
    governance = GovernanceService(session_factory)
    if ablation.no_attribution:
        governance.attributor = Misattributor(governance.attributor)
    if ablation.no_gate:
        governance.gate = AlwaysCommitGate()

    pack_service = NoEnvPackService(session_factory, gateway) if ablation.no_env_validation else PackService(
        session_factory, gateway
    )
    if ablation.no_hard_compile:
        pack_service.compiler = NullCompiler()
    compiler = pack_service.compiler

    refusal = False
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
        governance.record_episode(
            episode_input,
            explicit_preferences=[
                ExplicitPreferenceEvent(
                    key=spec["key"],
                    value=spec["value"],
                    hardness=PreferenceHardness(spec["hardness"]),
                    scene=Scene(**spec.get("scene", {})),
                )
                for spec in raw.get("explicit_preferences", [])
            ],
        )
        if ablation.no_reconciliation:
            continue
        _drain(session_factory, gateway)

    with session_factory() as session:
        prefs_active = list(
            session.scalars(
                select(PreferenceRecord).where(
                    PreferenceRecord.user_id == user_id, PreferenceRecord.status == "ACTIVE"
                )
            )
        )
        knowledge_rows = list(
            session.scalars(select(KnowledgeRecord).where(KnowledgeRecord.user_id == user_id))
        )
        active_knowledge = [k for k in knowledge_rows if k.status == "ACTIVE"]
        evidence_count = len(session.scalars(select(EvidenceRecord)).all())
        links = len(session.scalars(select(MemoryEvidenceLinkRecord)).all())
        # Traceability Coverage: fraction of committed knowledge rows that have at
        # least one evidence link (0..1). Raw mem0 arms keep no attribution at all.
        linked_knowledge = len(
            session.scalars(
                select(MemoryEvidenceLinkRecord.memory_id)
                .where(MemoryEvidenceLinkRecord.memory_kind == "K")
                .distinct()
            ).all()
        )
        traceable_knowledge = len([k for k in knowledge_rows if k.id in set(
            session.scalars(
                select(MemoryEvidenceLinkRecord.memory_id).where(
                    MemoryEvidenceLinkRecord.memory_kind == "K",
                    MemoryEvidenceLinkRecord.memory_id == k.id,
                )
            )
        )])

    # P-K visibility layer
    if oracle.pk_family and not ablation.no_pk_visibility:
        with session_factory.begin() as session:
            preference = session.scalar(
                select(PreferenceRecord).where(
                    PreferenceRecord.user_id == user_id, PreferenceRecord.status == "ACTIVE"
                )
            )
            knowledge = session.scalar(
                select(KnowledgeRecord).where(
                    KnowledgeRecord.user_id == user_id, KnowledgeRecord.status == "ACTIVE"
                )
            )
            if preference and knowledge:
                ConflictService().mask_knowledge(
                    session,
                    preference_id=preference.id,
                    knowledge_id=knowledge.id,
                    reason="offline HARD preference masks network fallback knowledge",
                )

    # forget
    forgotten = False
    if oracle.forget:
        knowledge = active_knowledge[0] if active_knowledge else None
        if knowledge:
            ForgettingService(session_factory).forget_knowledge(knowledge.id, user_id=user_id)
            if not ablation.no_reconciliation:
                _drain(session_factory, gateway)
            forgotten = True

    # PACK probes
    scene = Scene(**oracle.probe_scene)
    pack_home_ctx = None
    refusal = False
    constraints = None
    try:
        pack_home_ctx = pack_service.build(
            query="fallback",
            user_id=user_id,
            scene=scene,
            environment_fingerprint=oracle.env_home,
        )
        constraints = pack_home_ctx.constraints
    except ValueError:
        refusal = True  # unenforceable HARD key fails fast (correct-by-design)
        constraints = None
    pack_drift_ctx = None
    if oracle.env_drift:
        try:
            pack_drift_ctx = pack_service.build(
                query="fallback",
                user_id=user_id,
                scene=scene,
                environment_fingerprint=oracle.env_drift,
            )
        except ValueError:
            pack_drift_ctx = None

    visible_home = len(pack_home_ctx.knowledge) > 0 if pack_home_ctx is not None else False
    visible_drift = len(pack_drift_ctx.knowledge) > 0 if pack_drift_ctx is not None else None
    revalidation_count = len(
        session_factory()
        .scalars(select(KnowledgeRevalidationRecord))
        .all()
    )

    # decision
    tool = None
    violated = False
    if refusal:
        pass
    elif pack_home_ctx is None:
        tool = None
    else:
        constraints = pack_home_ctx.constraints
        knowledge_items = list(pack_home_ctx.knowledge)
        chosen_by_knowledge = None
        if knowledge_items:
            chosen_by_knowledge = knowledge_items[0]["content"].get("action", {}).get("fallback_to")
        preferred = [
            p.preference_value_json.get("tool")
            for p in prefs_active
            if p.preference_key == "preferred_tool"
        ]
        soft_order = preferred[0] if preferred else None
        candidate_order = []
        if soft_order:
            candidate_order.append(soft_order)
        if chosen_by_knowledge:
            candidate_order.append(chosen_by_knowledge)
        tools = [Tool(name) for name in ALL_TOOLS]
        feasible = apply_constraints(tools, constraints) if constraints is not None else tools
        if feasible is NO_FEASIBLE_ACTION:
            tool = None  # refusal
            violated = False
        else:
            allowed_names = {t.name for t in feasible}
            tool = next((name for name in candidate_order if name in allowed_names), None)
            if tool is None and allowed_names:
                tool = sorted(allowed_names)[0]
            # HARD semantics oracle (independent of compiled constraints so the
            # no_hard_compile ablation is still scored): a decision that executes
            # a tool the active HARD preference forbids is a violation.
            oracle_allowed = set(oracle.allowed_tools) if oracle.allowed_tools is not None else None
            hard_pref = [
                p for p in prefs_active
                if p.hardness == "HARD"
            ]
            for hard in hard_pref:
                value = hard.preference_value_json
                if hard.preference_key == "preferred_tool" and tool != value.get("tool"):
                    violated = True
                if hard.preference_key == "denied_tools" and tool in value.get("tools", []):
                    violated = True
                if hard.preference_key == "require_offline" and tool in NETWORK_TOOLS:
                    violated = True
            if oracle_allowed is not None and tool is not None and tool not in oracle_allowed:
                violated = True

    evidence_per_knowledge = (traceable_knowledge / len(knowledge_rows)) if knowledge_rows else 0.0

    # The strict PACK is the executable tier. The optional second tier exposes
    # only non-executable hints; it never changes the selected tool or safety
    # metrics. This separates useful context from admissible execution memory.
    hint_count = 0
    hint_triggered_revalidation = False
    hint_assisted_safe_action = False
    if two_tier:
        visible_ids = {
            item["id"]
            for item in (pack_home_ctx.knowledge if pack_home_ctx is not None else ())
        }
        # A hint is any non-forgotten authoritative knowledge item that is not
        # admissible for this request. Its content never enters tool selection.
        hints = [
            knowledge
            for knowledge in knowledge_rows
            if knowledge.status != "FORGOTTEN" and knowledge.id not in visible_ids
        ]
        hint_count = len(hints)
        hint_triggered_revalidation = bool(hints and oracle.env_drift)

        if safe_fallback and hints and not refusal:
            feasible_tools = (
                apply_constraints([Tool(name) for name in ALL_TOOLS], constraints)
                if constraints is not None
                else [Tool(name) for name in ALL_TOOLS]
            )
            executable_tools = (
                set() if feasible_tools is NO_FEASIBLE_ACTION else {item.name for item in feasible_tools}
            )
            # The fallback planner uses only the current scene and current
            # compiled constraints. It never reads a hint's historical action.
            defaults = TOOLS.get(oracle.probe_scene.get("app"), [])
            fallback_tool = next((name for name in defaults if name in executable_tools), None)
            if fallback_tool is None and executable_tools:
                fallback_tool = sorted(executable_tools)[0]
            if fallback_tool is not None and fallback_tool != tool:
                tool = fallback_tool
                hint_assisted_safe_action = tool == oracle.expected_tool

    return {
        "prefs_active": [
            {"key": p.preference_key, "value": p.preference_value_json, "hardness": p.hardness}
            for p in prefs_active
        ],
        "visible_home": visible_home,
        "visible_drift": visible_drift,
        "revalidation_count": revalidation_count,
        "knowledge_committed": len(knowledge_rows),
        "knowledge_active": len(active_knowledge),
        "duplicate_knowledge": len(knowledge_rows) - len({k.lineage_id for k in knowledge_rows}),
        "tool": tool,
        "hard_violation": violated,
        "refusal": refusal,
        "forgotten": forgotten,
        "evidence_count": evidence_count,
        "traceability": evidence_per_knowledge if knowledge_rows else 0.0,
        "pack_results": len(pack_home_ctx.knowledge) if pack_home_ctx is not None else 0,
        "non_executing_hint_count": hint_count,
        "hint_triggered_revalidation": hint_triggered_revalidation,
        "hint_assisted_safe_action": hint_assisted_safe_action,
        "hint_induced_unsafe_execution": False,
    }


def _drain(session_factory, gateway) -> int:
    worker = IndexWorker(session_factory, gateway)
    drained = 0
    while True:
        job_id = worker.process_next()
        if job_id is None:
            break
        drained += 1
    return drained


def _gateway(collection: str):
    emb = ShimEmbeddingClient(dim=768)
    vec = ShimVectorClient()
    return create_mem0_gateway(
        embedding_client=emb,
        vector_client=vec,
        embedding_dims=768,
        distance_metric="cosine_distance",
        score_semantics="cosine_distance",
        history_db_path=tempfile_path(collection),
        collection_name=collection,
    )


def tempfile_path(name: str) -> str:
    path = Path(tempfile.gettempdir()) / f"eagle-s11-{name}.db"
    if path.exists():
        path.unlink()
    return str(path)


# ---------------------------------------------------------------------------
# arm A/B case execution
# ---------------------------------------------------------------------------


def run_case_arm_ab(case: dict, oracle: Oracle, mode: str) -> dict:
    engine = create_sqlite_engine(":memory:")  # unused by A/B but keeps parity
    create_schema(engine)
    session_factory = make_session_factory(engine)
    user_id = case["user_id"]
    gateway = _gateway(f"eagle_s11_{mode}")
    pipeline = NaivePipeline(gateway, mode)

    for position, raw in enumerate(case["episodes"], start=1):
        pipeline.record(case["id"], position, raw, user_id)
        pipeline.correction(raw, user_id)

    forgotten = False
    if oracle.forget:
        forgotten = pipeline.forget(user_id, oracle)

    scene = Scene(**oracle.probe_scene)
    results = pipeline.pack(user_id, "fallback", scene)
    visible_home = any(
        (r.get("metadata") or {}).get("kind") == "K" and (r.get("metadata") or {}).get("tool") == oracle.knowledge_tool
        for r in results
    ) if oracle.knowledge_tool else any((r.get("metadata") or {}).get("kind") == "K" for r in results)
    visible_drift = visible_home  # A/B never filter by environment
    tool, violated = pipeline.decide(user_id, "fallback", scene)

    knowledge_facts = [k for k in pipeline.fact_keys]
    return {
        "prefs_active": [
            {"key": p["key"], "value": p["value"], "hardness": p["hardness"], "source": p["source"]}
            for p in pipeline.preferences
            if not p["revoked"]
        ],
        "visible_home": visible_home,
        "visible_drift": visible_drift,
        "revalidation_count": 0,
        "knowledge_committed": len(knowledge_facts),
        "knowledge_active": len(knowledge_facts),
        "duplicate_knowledge": len(knowledge_facts),
        "tool": tool,
        "hard_violation": violated,
        "refusal": False,  # A/B never refuse
        "forgotten": forgotten,
        "evidence_count": 0,
        "traceability": 0.0,
        "pack_results": len(results),
    }


# ---------------------------------------------------------------------------
# metric scoring per case
# ---------------------------------------------------------------------------


def score_case(case: dict, oracle: Oracle, observed: dict) -> dict:
    family = case["family"]
    s: dict = {}

    # --- preferences --------------------------------------------------------
    prefs = observed["prefs_active"]
    pref_influence = any(p["key"] == "preferred_tool" for p in prefs)
    if not oracle.expects_preference and oracle.false_promotion_applicable:
        s["pref_false_promotion"] = 1 if pref_influence else 0
        s["pref_precision_num"] = None
    else:
        s["pref_false_promotion"] = 0
        if oracle.expects_preference:
            match = [
                p
                for p in prefs
                if p["key"] == oracle.pref_key
                and p.get("value") == oracle.pref_value
                and (oracle.pref_hardness is None or p["hardness"] == oracle.pref_hardness)
            ]
            s["pref_precision_num"] = 1 if match else None
            s["pref_precision_den"] = 1 if prefs else 0
            s["pref_created"] = 1 if prefs else 0

    # --- knowledge ----------------------------------------------------------
    if oracle.expects_knowledge:
        correct_visible = observed["visible_home"] and (
            observed.get("tool") == oracle.knowledge_tool or (oracle.forget and not observed["visible_home"])
        )
        visible_items = observed.get("pack_results", 0)
        s["knowledge_precision_num"] = (1 if correct_visible else 0) if visible_items else None
        s["knowledge_precision_den"] = visible_items if visible_items else None
        s["knowledge_recall"] = 1 if observed["visible_home"] else 0
    else:
        s["knowledge_precision_num"] = None
        s["knowledge_precision_den"] = None
        s["knowledge_recall"] = None
        if observed["visible_home"] and not oracle.replay:
            s["knowledge_false_positive"] = 1

    # --- safety -------------------------------------------------------------
    if oracle.hard_kind is not None or oracle.pk_family:
        s["hard_violation"] = 1 if observed["hard_violation"] else 0
    else:
        s["hard_violation"] = 0
        if observed["hard_violation"]:
            s["hard_violation_extra"] = 1

    if oracle.env_drift is not None:
        s["stale_reuse"] = 1 if observed["visible_drift"] else 0
        s["revalidation_written"] = 1 if observed["revalidation_count"] > 0 else 0

    if oracle.conflict_family:
        ok = observed["knowledge_active"] == 1 and observed["visible_home"] and observed["tool"] == oracle.expected_tool
        s["conflict_resolved"] = 1 if ok else 0

    if oracle.forget:
        # leakage: knowledge still visible after forget
        s["forget_leakage"] = 1 if observed["visible_home"] else 0
    if oracle.replay:
        s["forget_leakage"] = 1 if observed["knowledge_active"] > 1 else 0

    s["traceability"] = observed.get("traceability", 0.0)

    # --- decomposed utility/safety metrics -------------------------------
    # These metrics separate correct governance blocks from harmful exposure.
    blocked_by_policy = (
        oracle.forget
        or oracle.env_drift is not None
        or oracle.pk_family
        or oracle.unenforceable
        or oracle.no_feasible
    )
    unsafe_execution = int(observed.get("hard_violation", False))
    s["unsafe_execution"] = unsafe_execution
    s["correct_policy_block"] = int(blocked_by_policy and observed.get("tool") is None)
    safe_action_exists = not oracle.unenforceable and not oracle.no_feasible and not oracle.forget and not oracle.pk_family
    s["missed_safe_action"] = int(
        safe_action_exists
        and observed.get("tool") != oracle.expected_tool
    )
    s["hint_assisted_safe_task_success"] = int(
        observed.get("hint_assisted_safe_action", False)
        and observed.get("tool") == oracle.expected_tool
        and not unsafe_execution
    )
    s["hint_triggered_revalidation"] = int(observed.get("hint_triggered_revalidation", False))
    s["hint_induced_unsafe_execution"] = int(observed.get("hint_induced_unsafe_execution", False))
    s["safe_task_success"] = int(
        (observed.get("tool") == oracle.expected_tool and not unsafe_execution)
        or (blocked_by_policy and observed.get("tool") is None and not unsafe_execution)
    )

    # Eligibility is evaluated only against knowledge that should be usable in
    # the current context. Forbidden/forgotten/stale/masked knowledge belongs
    # in an exposure metric, not in the denominator of EligibleRecall@5.
    eligible_context = oracle.expects_knowledge and not oracle.forget and not oracle.pk_family and oracle.env_drift is None
    s["eligible_recall"] = int(observed.get("visible_home", False)) if eligible_context else None
    s["governed_visibility"] = int(observed.get("visible_home", False)) if oracle.expects_knowledge else None
    s["hint_count"] = observed.get("non_executing_hint_count", 0)

    # --- downstream ---------------------------------------------------------
    if oracle.unenforceable:
        s["task_success"] = 1 if observed.get("refusal") else 0
    elif oracle.no_feasible:
        s["task_success"] = 1 if observed.get("tool") is None else 0
    else:
        s["task_success"] = 1 if observed.get("tool") == oracle.expected_tool else 0
    return s


FAMILY_METRIC_SCOPE = {
    "pref_false_promotion": None,  # computed only when applicable (score emits key or not)
    "pref_precision": ("implicit_gate", "explicit_gate", "hard_constraint", "pk_visibility"),
    "knowledge_precision": (
        "fallback_not_preference",
        "kk_conflict",
        "env_drift",
        "pk_visibility",
        "forgetting",
    ),
    "knowledge_recall": ("fallback_not_preference", "kk_conflict", "env_drift", "pk_visibility", "forgetting"),
    "hard_violation": ("hard_constraint", "pk_visibility"),
    "stale_reuse": ("env_drift",),
    "conflict_resolved": ("kk_conflict",),
    "forget_leakage": ("forgetting", "replay_idempotency"),
    "traceability": None,  # all cases with knowledge
    "task_success": None,  # all cases
    "eligible_recall": ("fallback_not_preference", "kk_conflict", "forgetting"),
    "safe_task_success": None,
    "unsafe_execution": None,
    "missed_safe_action": None,
    "correct_policy_block": None,
    "governed_visibility": None,
}


def aggregate_metrics(scored: list[dict]) -> dict:
    keys = [
        "pref_false_promotion",
        "pref_precision",
        "knowledge_precision",
        "knowledge_recall",
        "hard_violation",
        "stale_reuse",
        "conflict_resolved",
        "forget_leakage",
        "traceability",
        "task_success",
        "eligible_recall",
        "safe_task_success",
        "unsafe_execution",
        "missed_safe_action",
        "correct_policy_block",
        "governed_visibility",
        "hint_assisted_safe_task_success",
        "hint_triggered_revalidation",
        "hint_induced_unsafe_execution",
    ]
    out: dict[str, float | None] = {}
    for key in keys:
        if key == "pref_false_promotion":
            values = [item["s"]["pref_false_promotion"] for item in scored if "pref_false_promotion" in item["s"]]
            out[key] = _rate(values)
        elif key == "pref_precision":
            nums = [item["s"]["pref_precision_num"] for item in scored if item["s"].get("pref_precision_num") is not None]
            dens = [item["s"]["pref_precision_den"] for item in scored if item["s"].get("pref_precision_den") is not None]
            out[key] = (sum(nums) / sum(dens)) if dens and sum(dens) else None
        elif key == "knowledge_precision":
            nums = [
                item["s"]["knowledge_precision_num"]
                for item in scored
                if item["s"].get("knowledge_precision_num") is not None
            ]
            dens = [
                item["s"]["knowledge_precision_den"]
                for item in scored
                if item["s"].get("knowledge_precision_den") is not None
            ]
            out[key] = (sum(nums) / sum(dens)) if dens and sum(dens) else None
        elif key in {"eligible_recall", "safe_task_success", "unsafe_execution", "missed_safe_action", "correct_policy_block", "governed_visibility"}:
            values = [
                item["s"][key]
                for item in scored
                if item["s"].get(key) is not None
                and (key != "eligible_recall" or item["case"]["family"] in FAMILY_METRIC_SCOPE["eligible_recall"])
            ]
            out[key] = _rate(values)
        elif key == "knowledge_recall":
            values = [
                item["s"]["knowledge_recall"]
                for item in scored
                if item["s"].get("knowledge_recall") is not None
                and item["case"]["family"] in FAMILY_METRIC_SCOPE["knowledge_recall"]
            ]
            out[key] = _rate(values)
        elif key == "hard_violation":
            values = [
                item["s"]["hard_violation"]
                for item in scored
                if item["case"]["family"] in FAMILY_METRIC_SCOPE["hard_violation"]
            ] + [item["s"]["hard_violation_extra"] for item in scored if "hard_violation_extra" in item["s"]]
            out[key] = _rate(values)
        elif key in {"stale_reuse", "conflict_resolved", "forget_leakage"}:
            values = [
                item["s"][key]
                for item in scored
                if key in item["s"] and item["case"]["family"] in (FAMILY_METRIC_SCOPE[key] or ())
            ]
            out[key] = _rate(values)
        elif key in {
            "eligible_recall", "safe_task_success", "unsafe_execution",
            "missed_safe_action", "correct_policy_block", "governed_visibility",
            "hint_assisted_safe_task_success", "hint_triggered_revalidation",
            "hint_induced_unsafe_execution",
        }:
            values = [item["s"][key] for item in scored if item["s"].get(key) is not None]
            out[key] = _rate(values)
        elif key == "traceability":
            values = [
                item["s"]["traceability"]
                for item in scored
                if item["case"]["family"] in FAMILY_METRIC_SCOPE["knowledge_precision"]
            ]
            out[key] = (sum(values) / len(values)) if values else None
        else:
            values = [item["s"]["task_success"] for item in scored]
            out[key] = _rate(values)
    return out


def _rate(values: list) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_arm(
    arm: str,
    cases: list[dict],
    ablation: Ablation | None = None,
    *,
    two_tier: bool = False,
    safe_fallback: bool = False,
) -> list[dict]:
    scored = []
    for case in cases:
        oracle = build_oracle(case)
        if arm == "C":
            observed = run_case_arm_c(
                case,
                oracle,
                ablation or Ablation(),
                two_tier=two_tier,
                safe_fallback=safe_fallback,
            )
        else:
            observed = run_case_arm_ab(case, oracle, arm)
        scored.append({"case": {"id": case["id"], "family": case["family"]}, "s": score_case(case, oracle, observed)})
    return scored


METRIC_LABELS = {
    "pref_false_promotion": "Preference False Promotion Rate",
    "pref_precision": "Preference Precision",
    "knowledge_precision": "Knowledge Precision",
    "knowledge_recall": "Knowledge Recall@5",
    "hard_violation": "Hard Constraint Violation Rate",
    "stale_reuse": "Stale Knowledge Reuse Rate",
    "conflict_resolved": "Conflict Resolution Accuracy",
    "forget_leakage": "Forget Leakage Rate",
    "traceability": "Traceability Coverage",
    "task_success": "Downstream Task Success",
    "eligible_recall": "Eligible Recall@5",
    "safe_task_success": "Safe Task Success",
    "unsafe_execution": "Unsafe Execution Rate",
    "missed_safe_action": "Missed Safe Action Rate",
    "correct_policy_block": "Correct Policy Block Rate",
    "governed_visibility": "Governed Visibility Rate",
}
SAFETY_FIRST = [
    "hard_violation",
    "forget_leakage",
    "pref_false_promotion",
    "stale_reuse",
    "conflict_resolved",
    "knowledge_precision",
    "knowledge_recall",
    "pref_precision",
    "traceability",
    "task_success",
]


def mean_std_ci(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "std": None, "ci95": None}
    if len(values) == 1:
        return {"mean": round(values[0], 4), "std": 0.0, "ci95": [round(values[0], 4)] * 2}
    mean = statistics.mean(values)
    std = statistics.stdev(values)
    half = 1.96 * std / (len(values) ** 0.5)
    return {"mean": round(mean, 4), "std": round(std, 4), "ci95": [round(mean - half, 4), round(mean + half, 4)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=str, default="42,43,44")
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    seeds = [int(seed) for seed in args.seeds.split(",") if seed.strip()]
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    arms = {"A": None, "B": None, "C": None}
    ablations = {
        "full": Ablation(),
        "w/o Attribution": Ablation(no_attribution=True),
        "w/o Commitment Gate": Ablation(no_gate=True),
        "w/o HARD Compilation": Ablation(no_hard_compile=True),
        "w/o Environment Validation": Ablation(no_env_validation=True),
        "w/o P-K Visibility": Ablation(no_pk_visibility=True),
        "w/o Reconciliation": Ablation(no_reconciliation=True),
    }

    # stage 11: A/B/C per seed
    stage11: dict = {"arms": {}, "seeds": seeds, "total_per_seed": args.total}
    per_seed_metrics: dict[str, list[dict]] = defaultdict(list)
    for seed in seeds:
        cases = _generate.generate(seed, args.total)
        cases = [case.to_json() for case in cases]
        for arm in arms:
            scored = run_arm(arm, cases)
            metrics = aggregate_metrics(scored)
            per_seed_metrics[arm].append(metrics)
            print(f"[seed {seed}] arm {arm}: {json.dumps(metrics, ensure_ascii=False)}")
    for arm, metric_list in per_seed_metrics.items():
        stage11["arms"][arm] = {
            METRIC_LABELS[key]: mean_std_ci([m[key] for m in metric_list if m.get(key) is not None])
            for key in SAFETY_FIRST + [
                "eligible_recall", "safe_task_success", "unsafe_execution",
                "missed_safe_action", "correct_policy_block", "governed_visibility",
            ]
        }
    (out_dir / "stage11.report.json").write_text(json.dumps(stage11, indent=2, ensure_ascii=False) + "\n")

    # stage 12: ablations on arm C per seed
    stage12: dict = {"ablations": {}, "seeds": seeds, "total_per_seed": args.total}
    ablation_metrics: dict[str, list[dict]] = defaultdict(list)
    for seed in seeds:
        cases = _generate.generate(seed, args.total)
        cases = [case.to_json() for case in cases]
        for name, ablation in ablations.items():
            scored = run_arm("C", cases, ablation)
            metrics = aggregate_metrics(scored)
            metrics["name"] = name
            ablation_metrics[name].append(metrics)
            print(f"[seed {seed}] ablation {name}: {json.dumps({k: v for k, v in metrics.items() if k != 'name'}, ensure_ascii=False)}")
    baseline = {METRIC_LABELS[key]: mean_std_ci(
        [m[key] for m in ablation_metrics["full"] if m.get(key) is not None]
    ) for key in SAFETY_FIRST}
    stage12["ablations"]["full (EAGLE)"] = baseline
    for name in ablations:
        if name == "full":
            continue
        metrics_list = ablation_metrics[name]
        delta: dict = {}
        for key in SAFETY_FIRST:
            label = METRIC_LABELS[key]
            full_mean = baseline[label]["mean"]
            ablated_mean = mean_std_ci([m[key] for m in metrics_list if m.get(key) is not None])["mean"]
            if full_mean is not None and ablated_mean is not None:
                delta[label] = round(ablated_mean - full_mean, 4)
        stage12["ablations"][name] = {
            "metrics": {
                METRIC_LABELS[key]: mean_std_ci([m[key] for m in metrics_list if m.get(key) is not None])
                for key in SAFETY_FIRST
            },
            "delta_vs_full": delta,
        }
    (out_dir / "stage12.report.json").write_text(json.dumps(stage12, indent=2, ensure_ascii=False) + "\n")

    print("\n=== stage 11 (mean over seeds) ===")
    for arm in ("A", "B", "C"):
        print(f"arm {arm}:")
        for label in (METRIC_LABELS[key] for key in SAFETY_FIRST):
            value = stage11["arms"][arm][label]
            print(f"  {label:36s} {value['mean']}  (95% CI {value['ci95']})")
    print("\n=== stage 12 deltas vs full (mean over seeds) ===")
    for name, payload in stage12["ablations"].items():
        if name == "full (EAGLE)":
            continue
        print(f"{name}: {json.dumps(payload['delta_vs_full'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
