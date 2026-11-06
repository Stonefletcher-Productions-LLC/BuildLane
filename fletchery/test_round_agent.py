"""Agent for rounds: Best active worker by default, a dropdown of agents, and rounds stopped by a limit retry."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import projects
from agents import AgentPool, AgentSpec
from config import Config



class MergerChoiceTests(unittest.TestCase):
    def test_paused_preferred_agent_falls_back_to_best_active(self):
        pool = AgentPool([AgentSpec("codex-sol", kind="codex"), AgentSpec("codex-astra", kind="codex"),
                          AgentSpec("opus", kind="claude")], None, lambda s: None)
        pool.pause("codex-sol", 30, "usage limit")
        self.assertFalse(pool.available("codex-astra", "merger"))
        self.assertEqual(pool.merger("codex-sol"), "opus")
        self.assertEqual(pool.merger(""), "opus")


class LimitedWindowTests(unittest.TestCase):
    def test_limited_round_requeues_its_window_instead_of_blocking(self):
        import test_plans
        engine, gh, rt = test_plans.PlanTests.setup_engine(test_plans.PlanTests())
        row = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        engine.plans.update(row := engine.plans.rows[-1], state="running")
        engine.plans.finish_merge(row["id"], {"result": "limited", "summary": "codex-sol hit a limit"})
        self.assertEqual(engine.plans.rows[-1]["state"], "queued")
        engine.plans.update(engine.plans.rows[-1], state="running")
        engine.plans.finish_merge(row["id"], {"result": "failed", "summary": "build failed"})
        self.assertEqual(engine.plans.rows[-1]["state"], "blocked")

class RoundAgentSettingTests(unittest.TestCase):
    def profile(self, workers):
        d = Path(tempfile.mkdtemp())
        p = d / "p.profile.json"
        p.write_text(json.dumps({"name": "P", "repo": "a/b", "workers": {
            "stateDir": str(d / "state"), "agents": [{"name": "opus", "kind": "claude", "model": "claude-opus-5-5"},
                                                     {"name": "codex-sol", "kind": "codex", "enabled": False}],
            **workers}}), encoding="utf-8")
        return p

    def test_old_merge_agent_key_means_best_active_worker(self):
        cfg = Config.load(self.profile({"mergeAgent": "codex-sol"}))
        self.assertEqual(cfg.merge_agent, "")

    def test_dropdown_offers_best_active_first_then_each_agent(self):
        cfg = Config.load(self.profile({}))
        field = next(f for f in projects.read_settings(cfg)["fields"] if f["key"] == "merge_agent")
        self.assertEqual(field["type"], "select")
        self.assertEqual([o["value"] for o in field["options"]], ["", "opus", "codex-sol"])
        self.assertTrue(field["options"][0]["label"].startswith("Best active worker"))
        self.assertIn("off", field["options"][2]["label"])

    def test_choosing_an_agent_saves_round_agent_and_rejects_unknown_names(self):
        p = self.profile({"mergeAgent": "codex-sol"})
        cfg = Config.load(p)
        projects.apply_settings(cfg, {"merge_agent": "opus"})
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["workers"]["roundAgent"], "opus")
        self.assertEqual(Config.load(p).merge_agent, "opus")
        with self.assertRaises(ValueError):
            projects.apply_settings(cfg, {"merge_agent": "nobody"})
        projects.apply_settings(cfg, {"merge_agent": ""})
        self.assertEqual(Config.load(p).merge_agent, "")


if __name__ == "__main__":
    unittest.main()
