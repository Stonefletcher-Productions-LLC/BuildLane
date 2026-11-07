"""Profile tool permissions reach Claude sessions (settings.json never does); no network or agents."""
import json
from pathlib import Path
import tempfile
import unittest

from agents import AgentSpec
import clis
from config import Config
from runtime import TOOLS

BRIDGE = "Bash(E:/BlenderTools/BlenderMCP/.venv/Scripts/python.exe E:/BlenderTools/BlenderMCP/call.py:*)"


class WorkerPermissionTests(unittest.TestCase):
    def _cfg(self, workers):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "p.profile.json"
        p.write_text(json.dumps({"name": "t", "repo": "o/r", "workers": workers}), encoding="utf-8")
        return Config.load(p)

    def _rt(self, cfg):
        spec = AgentSpec(name="sonnet", kind="claude", model="claude-sonnet-5-5")
        return clis.make_runtime(spec, cfg, Path("."), None)

    def test_profile_keys_load(self):
        cfg = self._cfg({"workerAllowedTools": [BRIDGE], "workerDirs": ["E:/BlenderTools"]})
        self.assertEqual(cfg.worker_allowed_tools, [BRIDGE])
        self.assertEqual(cfg.worker_dirs, ["E:/BlenderTools"])

    def test_writers_get_extra_tools_readers_do_not(self):
        rt = self._rt(self._cfg({"workerAllowedTools": [BRIDGE, ""], "workerDirs": ["E:/BlenderTools"]}))
        self.assertEqual(rt.add_dirs, ["E:/BlenderTools"])
        for role in ("worker", "fixer", "merger"):
            self.assertIn(BRIDGE, rt.allowed_tools(role))
        for role in ("reviewer", "planner", "architect"):
            self.assertNotIn(BRIDGE, rt.allowed_tools(role))
        self.assertNotIn(BRIDGE, TOOLS["worker"]["allowed"])  # the shared table is not mutated

    def test_default_is_unchanged(self):
        rt = self._rt(self._cfg({}))
        self.assertEqual(rt.allowed_tools("worker"), TOOLS["worker"]["allowed"])
        self.assertEqual(rt.add_dirs, [])


class SessionStartFlakeTests(unittest.TestCase):
    def test_round_whose_session_never_started_files_no_task(self):
        from types import SimpleNamespace
        import rounds

        class NoGitHub:
            def __getattr__(self, name):
                raise AssertionError(f"GitHub must not be touched ({name})")

        cfg = SimpleNamespace(base_branch="Test-Staging")
        result = {"round": 39, "result": "crashed", "summary": "Exception: Control request timeout: initialize",
                  "stop_reason": "Exception: Control request timeout: initialize"}
        self.assertEqual(rounds.report_failure(NoGitHub(), cfg, result), 0)


if __name__ == "__main__":
    unittest.main()
