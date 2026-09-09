import asyncio

from eagle.db.orm import EpisodeRecord
from sqlalchemy import select

from eagle.adapters.os_tools.executor import ToolExecutionContext, execute_tool
from eagle.domain.scene import Scene
from eagle.governance import GovernanceService


class Tool:
    name = "wps"

    async def execute(self, **arguments):
        return arguments["path"]


def test_tool_executor_records_episode(session_factory):
    governance = GovernanceService(session_factory)
    context = ToolExecutionContext(
        user_id="u1",
        session_id="s1",
        request_text="open docx",
        scene=Scene(artifact_type="docx"),
        environment_fingerprint="linux:wps-1",
        execution_id="tool-execution-1",
    )

    result = asyncio.run(execute_tool(Tool(), {"path": "a.docx"}, context, governance))

    assert result == "a.docx"
    with session_factory() as session:
        episode = session.scalar(select(EpisodeRecord))
    assert episode.success is True
    assert episode.tool_name == "wps"
