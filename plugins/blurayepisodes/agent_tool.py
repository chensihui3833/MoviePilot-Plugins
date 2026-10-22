"""Agent result submission: no filesystem or transfer side effects."""
import json
from typing import Type

from pydantic import BaseModel, Field
from app.agent.tools.base import MoviePilotTool
from .agent_bridge import broker


class EpisodeMapping(BaseModel):
    episode: int = Field(strict=True, gt=0, description="全季集号，从1连续；不是盘内重置的集号")
    disc: int = Field(strict=True, gt=0, description="实际源光盘号")
    title: str = Field(description="已确认的单集标题")
    playlist: str = Field(description="相对整季目录的实际 MPLS 路径")
    video: str = Field(description="相对整季目录的实际 M2TS 路径")
    alternatives: list[str] = Field(default_factory=list, description="确认等价的其他地区播放列表路径")


class MappingInput(BaseModel):
    job_id: str = Field(description="本次后台分析任务提供的job_id")
    season: int = Field(strict=True, ge=1, le=999)
    episodes: list[EpisodeMapping]
    basis: str = Field(description="集序与播放入口对应的判断依据；不是只按编号、大小或时长排序")
    evidence: list[str] = Field(description="用于判断的菜单/导航证据文件，相对整季目录")
    unknown: list[str] = Field(default_factory=list, description="仍不能确认的问题；有未知项时不会整理")


class SubmitBlurayMapping(MoviePilotTool):
    name: str = "submit_bluray_mapping"
    description: str = (
        "Submit complete season-wide Blu-ray TV episode/video/playlist mapping for an active plugin job. "
        "Analyze the supplied download folder independently, including all its discs; preserve unknowns. "
        "This tool only returns decisions to the plugin, never organizes or changes files. "
        "Actual playlists, paths, whole-file ranges and hardlinks are separately validated by the plugin."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = MappingInput

    def get_tool_message(self, **kwargs):
        return "提交蓝光整季分集配对，等待文件校验"

    async def run(self, job_id, season, episodes, basis, evidence, unknown=None):
        try:
            rows = [row.model_dump() if isinstance(row, BaseModel) else row for row in episodes]
            broker.submit(job_id, self._session_id, {
                "season": season, "episodes": rows, "basis": basis,
                "evidence": evidence, "unknown": unknown or [],
            })
            return json.dumps({"submitted": True, "episodes": len(rows),
                               "message": "仅收到配对结果，尚未通过文件校验或执行整理"}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"submitted": False, "error": str(exc)}, ensure_ascii=False)
