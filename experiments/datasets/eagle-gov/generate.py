"""EAGLE-Gov: deterministic governance dataset generator.

Each item is a small episode script plus the invariant it exercises. Scripts are
executed by run_case() in runner.py against a live GovernanceService; the
expected block is the oracle (attribution, gate state, PACK visibility, and so
on). Generation is fully deterministic given --seed; no LLM and no external
service is involved, so this dataset runs before the Kylin smoke stage.

Case families (EAGLE-experiment.md sections 9/11/12 targets):
  fallback_not_preference   I1: fallback success never creates a Preference
  implicit_gate             I2: 3 choices across 2 sessions; corrections block
  explicit_gate             explicit HARD/SOFT preference commits once
  hard_constraint           I4: HARD compiles; empty action space -> NO_FEASIBLE_ACTION
  kk_conflict               single contradiction does not dethrone old knowledge
  env_drift                 I5: drift blocks + revalidation; home env keeps knowledge
  pk_visibility             I3: P-K mask hides knowledge without deleting it
  forgetting                I6: forget removes PACK visibility; user-scoped
  replay_idempotency        same execution_id replay adds no evidence
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

SEED_DEFAULT = 42

APPS = ["office", "browser", "terminal", "files"]
TASKS = {"office": "edit", "browser": "download", "terminal": "package-install", "files": "organize"}
ARTIFACTS = ["docx", "pdf", "tar.gz", "log"]

TOOLS = {
    "office": ["wps", "libreoffice", "onlyoffice"],
    "browser": ["firefox", "chromium", "wget"],
    "terminal": ["apt", "dnf", "pacman"],
    "files": ["mc", "ranger", "dolphin"],
}

NETWORK_TOOLS = {"wget", "apt", "dnf", "pacman", "firefox", "chromium"}


def env_for(app: str, variant: int) -> str:
    """Environment identity is per machine/app, not per tool: conflicts and drift
    are only meaningful when episodes run on the same environment."""
    return f"linux:{app}-{1 + variant % 3}"


@dataclass(frozen=True)
class Case:
    case_id: str
    family: str
    user_id: str
    episodes: tuple[dict, ...]
    expected: dict
    description: str

    def to_json(self) -> dict:
        return {
            "id": self.case_id,
            "family": self.family,
            "user_id": self.user_id,
            "episodes": list(self.episodes),
            "expected": self.expected,
            "description": self.description,
        }


class CaseFactory:
    """Builds the invariant families with a shared deterministic shape.

    The scene is drawn once per case: candidate identity includes the normalized
    scene, so a stable scene is what lets repeated episodes aggregate into one
    candidate.
    """

    def __init__(self, rng: random.Random, index: CaseIndex):
        self.rng = rng
        self.index = index

    def _draw_scene(self) -> dict:
        app = self.rng.choice(APPS)
        return {"app": app, "task": TASKS[app], "artifact_type": self.rng.choice(ARTIFACTS)}

    def _episode(
        self,
        scene: dict,
        session_id: str,
        tool: str,
        *,
        fallback_from: str | None = None,
        env_variant: int = 0,
        intervention: bool = False,
        correction: dict | None = None,
        execution_id: str | None = None,
    ) -> dict:
        episode = {
            "session_id": session_id,
            "request_text": f"{scene['task']} the {scene['artifact_type']} file",
            "scene": dict(scene),
            "tool_name": tool,
            "arguments_digest": hashlib.sha256(f"{tool}:{session_id}:{self.rng.random():.10f}".encode()).hexdigest(),
            "success": True,
            "environment_fingerprint": env_for(scene["app"], env_variant),
            "user_intervention": intervention or bool(correction),
        }
        if fallback_from is not None:
            episode["fallback_from"] = fallback_from
            episode["previous_error_code"] = "E_OPEN"
        if correction is not None:
            episode["user_correction"] = correction
        if execution_id is not None:
            episode["execution_id"] = execution_id
        return episode

    def fallback_not_preference(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, fallback = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1]
        episodes = (
            self._episode(scene, "s1", fallback, fallback_from=primary),
            self._episode(scene, "s2", fallback, fallback_from=primary),
        )
        return Case(
            case_id=self.index.next("fbnp"),
            family="fallback_not_preference",
            user_id=user_id,
            episodes=episodes,
            expected={
                "candidate_type": "K",
                "gate_state": "COMMITTED",
                "preference_created": False,
                "knowledge_created": True,
                "committed_count": 1,
            },
            description=f"two identical {primary}->fallback {fallback} successes must commit Knowledge only",
        )

    def implicit_gate(self, user_id: str) -> Case:
        scene = self._draw_scene()
        tool = self.rng.choice(TOOLS[scene["app"]])
        variant = self.rng.choice(["two_same_session", "three_two_sessions", "negative_correction"])
        if variant == "two_same_session":
            episodes = (
                self._episode(scene, "s1", tool, intervention=True),
                self._episode(scene, "s1", tool, intervention=True),
            )
            expected = {"gate_state": "PENDING", "committed_count": 0, "preference_created": False}
        elif variant == "three_two_sessions":
            episodes = (
                self._episode(scene, "s1", tool, intervention=True),
                self._episode(scene, "s1", tool, intervention=True),
                self._episode(scene, "s2", tool, intervention=True),
            )
            expected = {
                "gate_state": "COMMITTED",
                "committed_count": 1,
                "preference_created": True,
                "authorization_state": "INFERRED",
                "hardness": "SOFT",
            }
        else:
            other = next(t for t in TOOLS[scene["app"]] if t != tool)
            correction = {
                "candidate_type": "P",
                "key": "preferred_tool",
                "value": {"tool": tool},
                "scene": dict(scene),
            }
            episodes = (
                self._episode(scene, "s1", tool, intervention=True),
                self._episode(scene, "s1", tool, intervention=True),
                self._episode(scene, "s2", tool, intervention=True),
                self._episode(scene, "s3", other, correction=correction),
            )
            expected = {
                "committed_count": 1,
                "preference_created": True,
                "negative_evidence": 1,
                "candidate_state": "REJECTED",
                "preference_status": "REVOKED",
            }
        return Case(
            case_id=self.index.next("impl"),
            family="implicit_gate",
            user_id=user_id,
            episodes=episodes,
            expected=expected,
            description=f"implicit {tool} choice under {variant}",
        )

    def explicit_gate(self, user_id: str) -> Case:
        scene = self._draw_scene()
        tool = self.rng.choice(TOOLS[scene["app"]])
        hardness = self.rng.choice(["HARD", "SOFT"])
        explicit = {"key": "preferred_tool", "value": {"tool": tool}, "hardness": hardness, "scene": {}}
        episodes = (self._episode(scene, "s1", tool, execution_id=f"explicit-{self.index.peek('expl')}"),)
        episodes[0]["explicit_preferences"] = [explicit]
        return Case(
            case_id=self.index.next("expl"),
            family="explicit_gate",
            user_id=user_id,
            episodes=episodes,
            expected={
                "gate_state": "COMMITTED",
                "committed_count": 1,
                "preference_created": True,
                "authorization_state": "EXPLICIT",
                "hardness": hardness,
            },
            description=f"explicit {hardness} {tool} statement commits once",
        )

    def hard_constraint(self, user_id: str) -> Case:
        scene = self._draw_scene()
        tools = TOOLS[scene["app"]]
        tool = tools[0]
        kind = self.rng.choice(["preferred", "denied", "offline", "no_feasible", "unenforceable"])
        if kind == "preferred":
            preference = {"key": "preferred_tool", "value": {"tool": tool}, "hardness": "HARD", "scene": {}}
            allowed = [tool]
        elif kind == "denied":
            denied = tools[1]
            preference = {"key": "denied_tools", "value": {"tools": [denied]}, "hardness": "HARD", "scene": {}}
            allowed = sorted(set(TOOLS["office"] + TOOLS["browser"] + TOOLS["terminal"] + TOOLS["files"]) - {denied})
        elif kind == "offline":
            preference = {"key": "require_offline", "value": {"required": True}, "hardness": "HARD", "scene": {}}
            allowed = sorted(
                set(TOOLS["office"] + TOOLS["browser"] + TOOLS["terminal"] + TOOLS["files"]) - NETWORK_TOOLS
            )
        elif kind == "no_feasible":
            preference = {
                "key": "preferred_tool",
                "value": {"tool": "nonexistent-tool"},
                "hardness": "HARD",
                "scene": {},
            }
            allowed = None
        else:
            preference = self.rng.choice(
                [
                    {"key": "allowed_formats", "value": {"formats": ["pdf"]}, "hardness": "HARD", "scene": {}},
                    {
                        "key": "privacy_rule",
                        "value": {"rule": "no-network-for-sensitive"},
                        "hardness": "HARD",
                        "scene": {},
                    },
                ]
            )
            allowed = None
        episodes = (self._episode(scene, "s1", tools[1]),)
        episodes[0]["explicit_preferences"] = [preference]
        expected = {
            "committed_count": 1,
            "constraint_kind": kind,
            "allowed_tools": allowed,
            "no_feasible": kind == "no_feasible",
            "raises": kind == "unenforceable",
        }
        return Case(
            case_id=self.index.next("hard"),
            family="hard_constraint",
            user_id=user_id,
            episodes=episodes,
            expected=expected,
            description=f"HARD {kind} must physically filter planner tools",
        )

    def kk_conflict(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, a, b = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1], TOOLS[scene["app"]][2]
        episodes = (
            self._episode(scene, "s1", a, fallback_from=primary, env_variant=0),
            self._episode(scene, "s2", a, fallback_from=primary, env_variant=0),
            self._episode(scene, "s3", b, fallback_from=primary, env_variant=0),
        )
        return Case(
            case_id=self.index.next("kk"),
            family="kk_conflict",
            user_id=user_id,
            episodes=episodes,
            expected={
                "committed_count": 1,
                "old_knowledge_status": "ACTIVE",
                "conflicting_candidate_state": "DEFER",
            },
            description=f"single {a} vs {b} contradiction must DEFER, old knowledge stays ACTIVE",
        )

    def env_drift(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, fallback = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1]
        episodes = (
            self._episode(scene, "s1", fallback, fallback_from=primary, env_variant=0),
            self._episode(scene, "s2", fallback, fallback_from=primary, env_variant=0),
        )
        return Case(
            case_id=self.index.next("env"),
            family="env_drift",
            user_id=user_id,
            episodes=episodes,
            expected={
                "committed_count": 1,
                "home_environment": env_for(scene["app"], 0),
                "drift_environment": env_for(scene["app"], 1),
                "knowledge_visible_home_env": True,
                "knowledge_visible_drift_env": False,
                "knowledge_active_after_drift": True,
            },
            description=(
                f"knowledge from {env_for(scene['app'], 0)} must be blocked under "
                f"{env_for(scene['app'], 1)} with a revalidation request"
            ),
        )

    def pk_visibility(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, fallback = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1]
        network_tool = primary if primary in NETWORK_TOOLS else "wget"
        explicit = {"key": "require_offline", "value": {"required": True}, "hardness": "HARD", "scene": {}}
        episodes = (
            self._episode(scene, "s1", fallback, fallback_from=network_tool),
            self._episode(scene, "s2", fallback, fallback_from=network_tool),
            self._episode(scene, "s3", fallback),
        )
        episodes[2]["explicit_preferences"] = [explicit]
        return Case(
            case_id=self.index.next("pkv"),
            family="pk_visibility",
            user_id=user_id,
            episodes=episodes,
            expected={
                "committed_count": 2,
                "knowledge_visible_after_mask": False,
                "knowledge_status": "ACTIVE",
            },
            description="offline HARD preference masks network fallback knowledge without deletion",
        )

    def forgetting(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, fallback = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1]
        episodes = (
            self._episode(scene, "s1", fallback, fallback_from=primary),
            self._episode(scene, "s2", fallback, fallback_from=primary),
        )
        return Case(
            case_id=self.index.next("fgt"),
            family="forgetting",
            user_id=user_id,
            episodes=episodes,
            expected={
                "committed_count": 1,
                "visible_before_forget": True,
                "visible_after_forget": False,
                "cross_user_forget_rejected": True,
            },
            description="forget must remove PACK visibility immediately and stay user-scoped",
        )

    def replay_idempotency(self, user_id: str) -> Case:
        scene = self._draw_scene()
        primary, fallback = TOOLS[scene["app"]][0], TOOLS[scene["app"]][1]
        base = self._episode(scene, "s1", fallback, fallback_from=primary, execution_id="replayed-execution")
        episodes = (base, dict(base))
        return Case(
            case_id=self.index.next("rpl"),
            family="replay_idempotency",
            user_id=user_id,
            episodes=episodes,
            expected={
                "committed_count": 0,
                "episodes_stored": 1,
                "evidence_count": 1,
                "no_pending_job": True,
            },
            description="same execution_id replay returns original result without new evidence",
        )


class CaseIndex:
    def __init__(self):
        self._counters: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        value = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = value
        return f"gov-{prefix}-{value:04d}"

    def peek(self, prefix: str) -> str:
        return f"{self._counters.get(prefix, 0) + 1:04d}"


FAMILIES = [
    "fallback_not_preference",
    "implicit_gate",
    "explicit_gate",
    "hard_constraint",
    "kk_conflict",
    "env_drift",
    "pk_visibility",
    "forgetting",
    "replay_idempotency",
]


def generate(seed: int, total: int) -> list[Case]:
    rng = random.Random(seed)
    index = CaseIndex()
    factory = CaseFactory(rng, index)
    per_family = total // len(FAMILIES)
    remainder = total - per_family * len(FAMILIES)
    cases: list[Case] = []
    for family_index, family in enumerate(FAMILIES):
        count = per_family + (1 if family_index < remainder else 0)
        for _ in range(count):
            user_id = f"gov-user-{rng.randrange(1, 21):03d}"
            cases.append(getattr(factory, family)(user_id))
    return cases


def write_jsonl(cases: list[Case], out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(case.to_json(), ensure_ascii=False, sort_keys=True) for case in cases]
    payload = "\n".join(lines) + "\n"
    out_path.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--total", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "v1.jsonl")
    args = parser.parse_args()

    cases = generate(args.seed, args.total)
    digest = write_jsonl(cases, args.out)
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.family] = counts.get(case.family, 0) + 1

    manifest = {
        "dataset": "eagle-gov",
        "version": "v1",
        "seed": args.seed,
        "total": len(cases),
        "sha256": digest,
        "family_counts": counts,
    }
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
