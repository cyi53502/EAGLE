import hashlib
import json
import time
from dataclasses import dataclass

from eagle.domain.events import EpisodeInput, UserCorrectionEvent
from eagle.domain.scene import Scene


@dataclass(frozen=True)
class ToolExecutionContext:
    user_id: str
    session_id: str
    request_text: str
    scene: Scene
    environment_fingerprint: str
    execution_id: str
    fallback_from: str | None = None
    previous_error_code: str | None = None
    retry_count: int = 0
    user_intervention: bool = False
    user_correction: UserCorrectionEvent | None = None


async def execute_tool(tool, arguments: dict, context: ToolExecutionContext, governance):
    started = time.perf_counter()
    success = False
    result_class = None
    error_code = None
    try:
        result = await tool.execute(**arguments)
        success = True
        result_class = type(result).__name__
        return result
    except Exception as error:
        error_code = str(getattr(error, "code", type(error).__name__))
        raise
    finally:
        governance.record_episode(
            EpisodeInput(
                user_id=context.user_id,
                execution_id=context.execution_id,
                session_id=context.session_id,
                request_text=context.request_text,
                scene=context.scene,
                tool_name=tool.name,
                arguments_digest=hashlib.sha256(
                    json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                success=success,
                result_class=result_class,
                error_code=error_code,
                latency_ms=int((time.perf_counter() - started) * 1000),
                retry_count=context.retry_count,
                fallback_from=context.fallback_from,
                previous_error_code=context.previous_error_code,
                user_intervention=context.user_intervention,
                user_correction=context.user_correction,
                environment_fingerprint=context.environment_fingerprint,
            )
        )
