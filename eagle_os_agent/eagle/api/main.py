from dataclasses import asdict
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from eagle.conflict.service import ConflictService
from eagle.db.orm import (
    EvidenceRecord,
    KnowledgeRecord,
    MemoryEvidenceLinkRecord,
    PreferenceRecord,
    utc_now,
)
from eagle.domain.enums import CandidateType, PreferenceHardness, PreferenceStatus
from eagle.domain.events import EpisodeInput, ExplicitPreferenceEvent, UserCorrectionEvent
from eagle.domain.scene import Scene
from eagle.health import HealthService


class ScenePayload(BaseModel):
    app: str | None = None
    task: str | None = None
    artifact_type: str | None = None

    def to_domain(self) -> Scene:
        return Scene(app=self.app, task=self.task, artifact_type=self.artifact_type)


class ExplicitPreferencePayload(BaseModel):
    key: str = Field(min_length=1)
    value: dict
    hardness: PreferenceHardness
    scene: ScenePayload = Field(default_factory=ScenePayload)


class UserCorrectionPayload(BaseModel):
    candidate_type: CandidateType
    key: str = Field(min_length=1)
    value: dict
    scene: ScenePayload = Field(default_factory=ScenePayload)


class EpisodePayload(BaseModel):
    execution_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request_text: str
    scene: ScenePayload = Field(default_factory=ScenePayload)
    tool_name: str = Field(min_length=1)
    arguments_digest: str = Field(min_length=1)
    success: bool
    environment_fingerprint: str = Field(min_length=1)
    result_class: str | None = None
    error_code: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    fallback_from: str | None = None
    previous_error_code: str | None = None
    user_intervention: bool = False
    user_correction: UserCorrectionPayload | None = None
    explicit_preferences: list[ExplicitPreferencePayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fallback_evidence(self):
        if self.fallback_from is not None and self.previous_error_code is None:
            raise ValueError("previous_error_code is required when fallback_from is set")
        if self.previous_error_code is not None and self.fallback_from is None:
            raise ValueError("fallback_from is required when previous_error_code is set")
        return self


class PackPayload(BaseModel):
    query: str = Field(min_length=1)
    scene: ScenePayload = Field(default_factory=ScenePayload)
    environment_fingerprint: str = Field(min_length=1)
    top_k: int = Field(default=5, gt=0, le=100)


class ForgetPayload(BaseModel):
    memory_kind: Literal["P", "K"]
    memory_id: str = Field(min_length=1)


def create_app(*, session_factory, governance, pack, forgetting, gateway, authenticate) -> FastAPI:
    app = FastAPI(title="EAGLE OS Agent", version="0.1.0")

    def authenticated_user(user_id=Depends(authenticate)) -> str:  # noqa: B008 - FastAPI dependency
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user_id

    @app.post("/episodes")
    def create_episode(payload: EpisodePayload, user_id: str = Depends(authenticated_user)):
        try:
            result = governance.record_episode(
                EpisodeInput(
                    user_id=user_id,
                    session_id=payload.session_id,
                    request_text=payload.request_text,
                    scene=payload.scene.to_domain(),
                    tool_name=payload.tool_name,
                    arguments_digest=payload.arguments_digest,
                    success=payload.success,
                    environment_fingerprint=payload.environment_fingerprint,
                    execution_id=payload.execution_id,
                    result_class=payload.result_class,
                    error_code=payload.error_code,
                    latency_ms=payload.latency_ms,
                    retry_count=payload.retry_count,
                    fallback_from=payload.fallback_from,
                    previous_error_code=payload.previous_error_code,
                    user_intervention=payload.user_intervention,
                    user_correction=(
                        UserCorrectionEvent(
                            candidate_type=payload.user_correction.candidate_type,
                            key=payload.user_correction.key,
                            value=payload.user_correction.value,
                            scene=payload.user_correction.scene.to_domain(),
                        )
                        if payload.user_correction is not None
                        else None
                    ),
                ),
                explicit_preferences=[
                    ExplicitPreferenceEvent(
                        key=item.key,
                        value=item.value,
                        hardness=item.hardness,
                        scene=item.scene.to_domain(),
                    )
                    for item in payload.explicit_preferences
                ],
            )
        except ValueError as error:
            status_code = 404 if str(error).endswith("not found") else 409
            raise HTTPException(status_code=status_code, detail=str(error)) from error
        return asdict(result)

    @app.post("/pack")
    def build_pack(payload: PackPayload, user_id: str = Depends(authenticated_user)):
        return asdict(
            pack.build(
                query=payload.query,
                user_id=user_id,
                scene=payload.scene.to_domain(),
                environment_fingerprint=payload.environment_fingerprint,
                top_k=payload.top_k,
            )
        )

    @app.get("/preferences")
    def list_preferences(user_id: str = Depends(authenticated_user)):
        with session_factory() as session:
            rows = session.scalars(select(PreferenceRecord).where(PreferenceRecord.user_id == user_id))
            return [
                {
                    "id": row.id,
                    "key": row.preference_key,
                    "value": row.preference_value_json,
                    "hardness": row.hardness,
                    "scene": row.scene_json,
                    "version": row.version,
                    "status": row.status,
                }
                for row in rows
            ]

    @app.get("/knowledge")
    def list_knowledge(user_id: str = Depends(authenticated_user)):
        with session_factory() as session:
            rows = session.scalars(select(KnowledgeRecord).where(KnowledgeRecord.user_id == user_id))
            return [
                {
                    "id": row.id,
                    "type": row.knowledge_type,
                    "content": row.content_json,
                    "scene": row.scene_json,
                    "environment_fingerprint": row.environment_fingerprint,
                    "version": row.version,
                    "status": row.status,
                }
                for row in rows
            ]

    @app.get("/memories/{memory_id}/evidence")
    def get_evidence(memory_id: str, user_id: str = Depends(authenticated_user)):
        with session_factory() as session:
            owned = session.scalar(
                select(PreferenceRecord.id).where(
                    PreferenceRecord.id == memory_id,
                    PreferenceRecord.user_id == user_id,
                )
            ) or session.scalar(
                select(KnowledgeRecord.id).where(
                    KnowledgeRecord.id == memory_id,
                    KnowledgeRecord.user_id == user_id,
                )
            )
            if owned is None:
                raise HTTPException(status_code=404, detail="Memory not found")
            rows = session.scalars(
                select(EvidenceRecord)
                .join(
                    MemoryEvidenceLinkRecord,
                    MemoryEvidenceLinkRecord.evidence_id == EvidenceRecord.id,
                )
                .where(
                    MemoryEvidenceLinkRecord.memory_id == memory_id,
                )
            )
            return [
                {
                    "id": row.id,
                    "episode_id": row.episode_id,
                    "direction": row.direction,
                    "strength": row.strength,
                    "contribution": row.contribution,
                    "reason": row.attribution_reason,
                }
                for row in rows
            ]

    @app.post("/memories/forget")
    def forget_memory(payload: ForgetPayload, user_id: str = Depends(authenticated_user)):
        try:
            if payload.memory_kind == "K":
                forgetting.forget_knowledge(payload.memory_id, user_id=user_id)
                status = "FORGETTING"
            elif payload.memory_kind == "P":
                forgetting.forget_preference(payload.memory_id, user_id=user_id)
                status = "FORGOTTEN"
        except ValueError as error:
            status_code = 404 if str(error).endswith("not found") else 409
            raise HTTPException(status_code=status_code, detail=str(error)) from error
        return {"status": status}

    @app.post("/preferences/{preference_id}/revoke")
    def revoke_preference(preference_id: str, user_id: str = Depends(authenticated_user)):
        with session_factory.begin() as session:
            preference = session.scalar(
                select(PreferenceRecord).where(
                    PreferenceRecord.id == preference_id,
                    PreferenceRecord.user_id == user_id,
                )
            )
            if preference is None:
                raise HTTPException(status_code=404, detail="Preference not found")
            if preference.status != PreferenceStatus.ACTIVE.value:
                raise HTTPException(status_code=409, detail="Preference is not ACTIVE")
            preference.status = PreferenceStatus.REVOKED.value
            preference.updated_at = utc_now()
            ConflictService().unmask_preference(session, preference.id)
        return {"status": "REVOKED"}

    @app.get("/health")
    def health():
        return HealthService(session_factory, gateway).check().to_dict()

    @app.get("/capabilities")
    def capabilities():
        return {
            "embedder": "kylin",
            "vector_store": "kylin",
            "llm": "noop",
            "vector_capabilities": sorted(gateway.capability_report.supported),
            "capability_errors": [
                {"capability": name, "error": "probe_failed"} for name, _error in gateway.capability_report.errors
            ],
        }

    return app
