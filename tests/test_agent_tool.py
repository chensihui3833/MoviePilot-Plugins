import asyncio
import importlib
import json
import sys
import types
import unittest
from unittest.mock import patch
from pydantic import BaseModel, PrivateAttr

from test_agent_mapping import bridge


class FakeMoviePilotTool(BaseModel):
    _session_id: str = PrivateAttr()

    def __init__(self, session_id, **kwargs):
        super().__init__(**kwargs)
        self._session_id = session_id


class AgentToolTest(unittest.TestCase):
    def test_pydantic_schema_submission_and_wrong_session_rejection(self):
        fake = types.SimpleNamespace(MoviePilotTool=FakeMoviePilotTool)
        with patch.dict(sys.modules, {"app.agent.tools.base": fake}):
            module = importlib.import_module("agent_test_package.agent_tool")
        job, prefix = bridge.broker.open()
        self.addCleanup(bridge.broker.close, job)
        payload = module.MappingInput.model_validate({
            "job_id": job, "season": 1, "episodes": [{"episode": 1, "disc": 1,
            "title": "Pilot", "playlist": "DISC1/BDMV/PLAYLIST/00001.mpls",
            "video": "DISC1/BDMV/STREAM/00005.m2ts"}],
            "basis": "actual menu mapping", "evidence": ["DISC1/menu.xml"], "unknown": []})
        tool = module.SubmitBlurayMapping(session_id="unrelated_session")
        self.assertFalse(json.loads(asyncio.run(tool.run(**payload.model_dump())))["submitted"])
        tool = module.SubmitBlurayMapping(session_id=prefix + "_abcd__")
        response = json.loads(asyncio.run(tool.run(**payload.model_dump())))
        self.assertTrue(response["submitted"])
        self.assertEqual(bridge.broker.take(job)["episodes"][0]["video"], "DISC1/BDMV/STREAM/00005.m2ts")
        self.assertIn("episodes", tool.args_schema.model_json_schema()["properties"])


if __name__ == "__main__":
    unittest.main()
