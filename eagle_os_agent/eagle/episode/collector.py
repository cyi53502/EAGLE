from dataclasses import asdict

from eagle.db.orm import EpisodeRecord
from eagle.domain.events import EpisodeInput


class EpisodeCollector:
    def collect(self, session, episode: EpisodeInput, ingest_fingerprint: str) -> EpisodeRecord:
        record = EpisodeRecord(
            execution_id=episode.execution_id,
            ingest_fingerprint=ingest_fingerprint,
            user_id=episode.user_id,
            session_id=episode.session_id,
            scene_json=episode.scene.normalized(),
            request_text=episode.request_text,
            tool_name=episode.tool_name,
            arguments_digest=episode.arguments_digest,
            success=episode.success,
            result_class=episode.result_class,
            error_code=episode.error_code,
            latency_ms=episode.latency_ms,
            retry_count=episode.retry_count,
            fallback_from=episode.fallback_from,
            previous_error_code=episode.previous_error_code,
            user_intervention=episode.user_intervention,
            user_correction_json=(asdict(episode.user_correction) if episode.user_correction is not None else None),
            environment_fingerprint=episode.environment_fingerprint,
        )
        session.add(record)
        session.flush()
        return record
