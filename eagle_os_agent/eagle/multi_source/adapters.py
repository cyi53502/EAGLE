"""Multi-source ingestion: tool + behavior + manual config → EpisodeInput.

Each adapter normalizes its raw dict into ``EpisodeInput`` then through
``clean_and_validate`` (dedup/quality/format) before ``GovernanceService``.
Covers competition requirement §1.(1) 多源数据整合.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent
from eagle.domain.scene import Scene
from eagle.domain.enums import PreferenceHardness


@dataclass(frozen=True)
class NormalizedEvent:
    source: str  # tool | behavior | manual
    episode: EpisodeInput
    explicit_prefs: tuple[ExplicitPreferenceEvent, ...] = ()
    quality: float = 1.0
    raw: dict | None = None


def _quality_ok(raw: dict) -> float:
    score = 1.0
    if not raw.get("user_id") or not raw.get("session_id"):
        score -= 0.6
    if not raw.get("tool_name") and not raw.get("behavior_type") and not raw.get("config_key"):
        score -= 0.4
    return max(0.0, score)


def clean_and_validate(events: list[NormalizedEvent], *, min_quality: float = 0.5) -> list[NormalizedEvent]:
    seen: set[str] = set()
    out: list[NormalizedEvent] = []
    for ev in events:
        if ev.quality < min_quality:
            continue
        fp = hashlib.sha256(f"{ev.episode.user_id}|{ev.episode.execution_id}".encode()).hexdigest()
        if fp in seen:
            continue
        seen.add(fp)
        out.append(ev)
    return out


class BehaviorAdapter:
    """User behavior stream (mouse/key/WPS ops) → EpisodeInput."""

    def to_event(self, raw: dict) -> NormalizedEvent:
        scene = Scene.from_dict(raw.get("scene"))
        ep = EpisodeInput(
            user_id=str(raw["user_id"]),
            session_id=str(raw["session_id"]),
            request_text=str(raw.get("action_seq") or raw.get("request_text") or ""),
            scene=scene,
            tool_name=f"behavior::{raw.get('behavior_type','unknown')}",
            arguments_digest=hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16],
            success=bool(raw.get("success", True)),
            environment_fingerprint=str(raw.get("environment_fingerprint") or raw.get("env_fp") or "unknown"),
            latency_ms=int(raw.get("latency_ms", 0)),
            user_intervention=True,
        )
        return NormalizedEvent(source="behavior", episode=ep, quality=_quality_ok(raw), raw=raw)


class ManualConfigAdapter:
    """Manual configuration change → EpisodeInput + optional ExplicitPreferenceEvent."""

    def to_event(self, raw: dict) -> NormalizedEvent:
        scene = Scene.from_dict(raw.get("scene"))
        explicit: list[ExplicitPreferenceEvent] = []
        if raw.get("config_key"):
            hardness = PreferenceHardness.HARD if raw.get("hardness") == "HARD" else PreferenceHardness.SOFT
            explicit.append(ExplicitPreferenceEvent(
                key=str(raw["config_key"]), value=dict(raw.get("config_value") or {}),
                hardness=hardness, scene=scene,
            ))
        ep = EpisodeInput(
            user_id=str(raw["user_id"]),
            session_id=str(raw["session_id"]),
            request_text=str(raw.get("request_text") or f"config {raw.get('config_key')}"),
            scene=scene,
            tool_name=f"config::{raw.get('config_key','unknown')}",
            arguments_digest=hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16],
            success=True,
            environment_fingerprint=str(raw.get("environment_fingerprint") or "manual"),
        )
        return NormalizedEvent(source="manual", episode=ep, explicit_prefs=tuple(explicit), quality=_quality_ok(raw), raw=raw)


def ingest_multi_source(
    raw_events: list[dict],
    *,
    source_hint: str = "auto",
) -> list[NormalizedEvent]:
    """Auto-dispatch raw dicts by keys, normalize, clean."""
    behavior = BehaviorAdapter()
    manual = ManualConfigAdapter()
    out: list[NormalizedEvent] = []
    for raw in raw_events:
        if source_hint == "behavior" or "behavior_type" in raw:
            out.append(behavior.to_event(raw))
        elif source_hint == "manual" or "config_key" in raw:
            out.append(manual.to_event(raw))
        else:
            # tool events are already EpisodeInput elsewhere; wrap minimally
            # caller should use governance directly for tool episodes
            out.append(behavior.to_event(raw))
    return clean_and_validate(out)
