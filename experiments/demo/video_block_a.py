#!/usr/bin/env python
"""Video Block A — 开箱即用：记忆方案接入效果（录制用脚本）。

录制方式：``python experiments/demo/video_block_a.py``，每个环节有横幅标题与
停顿（PAUSE 秒），边录边口播。全程 shim 模式（KYLIN_USE_SHIM=1），离线可跑、
确定性输出；真实麒麟路径在 Block B 用既有 report.json 展示。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2] / "eagle_os_agent"
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("KYLIN_USE_SHIM", "1")

PAUSE = float(os.getenv("VIDEO_PAUSE", "2.5"))


def banner(n: str, title: str) -> None:
    print("\n" + "=" * 74)
    print(f"▶ [{n}] {title}")
    print("=" * 74)
    time.sleep(PAUSE)


def pprint(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
    time.sleep(PAUSE)


def main() -> None:
    print(f"EAGLE 演示 Block A · Python {sys.version.split()[0]} · {os.uname().sysname} {os.uname().release}")

    # -- 1. 麒麟适配层能力自检 -------------------------------------------------
    banner("1/7", "麒麟适配层能力自检（11 探针，shim 模式离线可跑）")
    from eagle.adapters.kylin.capabilities import probe_vector_capabilities
    from eagle.adapters.kylin.vector_shim import ShimVectorClient

    probe = probe_vector_capabilities(ShimVectorClient(), dimension=768, metric="cosine_distance")
    print("supported:", sorted(probe.supported))
    print("errors:", probe.errors)
    assert not probe.errors, "capability gate must pass"

    # -- 2. 三行接入 Mem0 网关 -------------------------------------------------
    banner("2/7", "初始化：三行接入 Mem0 网关（SQLite 权威 + 向量语义索引）")
    from eagle.adapters.kylin.embedding_shim import ShimEmbeddingClient
    from eagle.adapters.kylin.vector_shim import ShimVectorClient
    from eagle.bootstrap import create_mem0_gateway
    from eagle.db import create_schema, create_sqlite_engine, make_session_factory

    engine = create_sqlite_engine(":memory:")
    create_schema(engine)
    sf = make_session_factory(engine)
    gw = create_mem0_gateway(
        embedding_client=ShimEmbeddingClient(dim=768),
        vector_client=ShimVectorClient(),
        embedding_dims=768,
        distance_metric="cosine_distance",
        score_semantics="cosine_distance",
        history_db_path="/tmp/eagle_video_history.db",
        collection_name="eagle_video",
    )
    print("gateway.backend:", gw.backend)
    print("embedding_ready:", gw.embedding_ready(), "| vector_ready:", gw.vector_ready())

    # -- 3. 多源数据接入 -------------------------------------------------------
    banner("3/7", "多源数据接入：工具执行 / 用户行为流 / 手动配置 统一标准化")
    from eagle.domain.events import EpisodeInput
    from eagle.domain.scene import Scene
    from eagle.governance import GovernanceService
    from eagle.multi_source.adapters import BehaviorAdapter, ManualConfigAdapter

    gov = GovernanceService(sf)
    env = "linux:noble:wps-1"
    scene = Scene(app="office", task="edit", artifact_type="docx")

    # 工具执行结果（回退成功的经验，fallback 归因）
    ep_tool = EpisodeInput(
        user_id="u1", session_id="s1", request_text="edit docx",
        scene=scene, tool_name="libreoffice", arguments_digest="d1",
        success=True, environment_fingerprint=env,
        fallback_from="wps", previous_error_code="E_OPEN", execution_id="v-s1-1",
    )
    # 用户行为流（行为适配器归一化）
    behavior = BehaviorAdapter().to_event({
        "user_id": "u1", "session_id": "s1", "behavior_type": "save_as_pdf",
        "scene": {"app": "office", "task": "export"}, "success": True,
        "environment_fingerprint": env,
    })
    # 手动配置（生成显式 HARD 偏好）
    manual = ManualConfigAdapter().to_event({
        "user_id": "u1", "session_id": "s1", "config_key": "preferred_tool",
        "config_value": {"tool": "wps"}, "hardness": "HARD",
        "scene": {"app": "office", "task": "edit"},
        "environment_fingerprint": env,
    })
    print("-- 工具执行 episode:", ep_tool.execution_id)
    print("-- 行为流 normalized :", behavior.source, behavior.episode.tool_name)
    print("-- 手动配置 normalized:", manual.source, manual.episode.tool_name,
          "| explicit:", [(p.key, p.value, p.hardness.value) for p in manual.explicit_prefs])
    r_tool = gov.record_episode(ep_tool)
    r_beh = gov.record_episode(behavior.episode, explicit_preferences=behavior.explicit_prefs)
    r_cfg = gov.record_episode(manual.episode, explicit_preferences=manual.explicit_prefs)
    print("-- 工具经验 committed:", list(r_tool.committed_memory_ids))
    print("-- 行为  经验 committed:", list(r_beh.committed_memory_ids))
    print("-- 配置→偏好 committed:", list(r_cfg.committed_memory_ids))

    # -- 4. 跨会话确认 → 长期知识提交 -----------------------------------------
    banner("4/7", "跨会话确认：第二个独立 session 同条件成功 → 承诺门控提交长期知识")
    ep_tool2 = EpisodeInput(
        user_id="u1", session_id="s2", request_text="edit docx",
        scene=scene, tool_name="libreoffice", arguments_digest="d2",
        success=True, environment_fingerprint=env,
        fallback_from="wps", previous_error_code="E_OPEN", execution_id="v-s2-1",
    )
    r2 = gov.record_episode(ep_tool2)
    print("s2 提交 committed:", list(r2.committed_memory_ids), "(首个 session 只进候选, 不提交)")

    # 索引 worker 落盘到向量库（mem0_id 就绪后 PACK 才能召回）
    from eagle.outbox.worker import IndexWorker

    worker = IndexWorker(sf, gw)
    drained = 0
    while worker.process_next() is not None:
        drained += 1
    drained_before = drained
    print(f"索引 worker 落盘 {drained} 条 → mem0_id 就绪")

    # -- 5. PACK 检索：历史经验复用 -------------------------------------------
    banner("5/7", "PACK 检索：历史经验跨会话复用 + 证据溯源 + 约束编译")
    from eagle.pack.service import PackService

    pack = PackService(sf, gw)
    ctx = pack.build(
        query="edit docx fallback",
        user_id="u1", scene=scene, environment_fingerprint=env, top_k=5,
    )
    print("-- 编译后的约束 constraints:")
    pprint({"constraints": asdict_simple(ctx.constraints)})
    print("-- 召回知识（历史经验）:")
    for k in ctx.knowledge:
        action = k["content"].get("action", {})
        print(f"   id={k['id'][:8]} type={k['type']} 回退到={action.get('fallback_to')} score={k['score']:.3f}")
    print("-- 证据溯源:", [(e["memory_id"][:8], e["reason"][:40], e["contribution"]) for e in ctx.evidence])
    assert ctx.knowledge, "PACK must reuse the committed historical experience"

    # -- 6. 自然语言指令遗忘 ---------------------------------------------------
    banner("6/7", "自然语言指令驱动的精准遗忘（无 LLM，端侧确定性解析）")
    from eagle.forgetting.nlu import resolve_targets
    from eagle.forgetting.service import ForgettingService

    forgetting = ForgettingService(sf)
    with sf() as session:
        targets = resolve_targets("忘掉使用 wps 编辑文档的工作流知识", "u1", session)
    print("NL 解析 targets:", {k: [t[:8] for t in v] for k, v in targets.items()})
    for kid in targets["K"]:
        forgetting.forget_knowledge(kid, user_id="u1")
    # 遗忘后 PACK 不可见
    ctx_after = pack.build(
        query="edit docx fallback", user_id="u1",
        scene=scene, environment_fingerprint=env, top_k=5,
    )
    print("遗忘后 PACK 召回条数:", len(ctx_after.knowledge), "(应为 0：FORGETTING 立即不可见)")
    while worker.process_next() is not None:
        drained += 1
    print(f"worker 收敛遗忘删除作业 {drained - drained_before} 条 → 向量/内容彻底擦除")

    # -- 7. 健康与一致性 -------------------------------------------------------
    banner("7/7", "健康检查 / 一致性收敛（outbox + 300s 租约）")
    from eagle.health import HealthService

    report = HealthService(sf, gw).check()
    print("health:", report.status, "| database_ready:", report.database_ready,
          "| pending_jobs:", report.pending_jobs, "| reconciliation_required:", report.reconciliation_required)
    print("\n✅ Block A 完成 — 接入→多源→治理→复用→遗忘→健康 全链路可录")
    return 0


def asdict_simple(c) -> dict:
    # PlannerConstraint holds frozensets; convert to lists for JSON display.
    return {
        "allowed_tools": sorted(c.allowed_tools or ()),
        "denied_tools": sorted(c.denied_tools or ()),
        "require_offline": c.require_offline,
        "allowed_formats": sorted(c.allowed_formats or ()),
        "privacy_rules": list(c.privacy_rules or ()),
    }


if __name__ == "__main__":
    raise SystemExit(main())
