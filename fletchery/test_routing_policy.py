"""Provider preference, restriction persistence and atomic dashboard policy updates; no agents/network."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents import AgentPool, specs_from
from config import Config
from server import Dashboard


class RoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "project.json"
        self.path.write_text("{}", encoding="utf-8")
        self.raw = [{"name": "flash", "kind": "opencode", "model": "opencode/free", "tier": 1},
                    {"name": "codex", "kind": "codex", "tier": 3},
                    {"name": "claude", "kind": "claude", "tier": 4}]
        self.routing = {"mode": "lists", "medium": ["flash", "codex", "claude"],
                        "preferredKindsByLevel": {"medium": ["codex", "claude"]}}
        self.pool = AgentPool(specs_from(self.raw), copy.deepcopy(self.routing), lambda _: None)
        self.cfg = Config(agents=copy.deepcopy(self.raw), routing=copy.deepcopy(self.routing))
        self.cfg._path = self.path
        self.fm = SimpleNamespace(ctx=SimpleNamespace(agents=self.pool, log=lambda _: None), cfg=self.cfg,
                                  wake=lambda: None, check_accounts=lambda **_: None)
        self.dashboard = Dashboard(self.fm, 0)

    def choose(self, body=""):
        return self.pool.choose("Implement feature", body, ["difficulty:medium"])

    def test_list_mode_prefers_native_and_falls_back_when_unavailable(self):
        self.assertEqual(self.choose().agent, "codex")
        self.pool.specs["codex"].roles = ["reviewer"]
        self.assertEqual(self.choose().agent, "claude")
        self.pool.specs["claude"].levels = ["easy"]
        self.assertEqual(self.choose().agent, "flash")
        self.pool.specs["flash"].levels = ["easy"]
        self.assertIsNone(self.choose())

    def test_busy_or_signed_out_preferred_agent_does_not_block_fallback(self):
        self.pool.specs["codex"].max_parallel = 1
        self.pool.busy["codex"] = 1
        self.pool.accounts["claude"] = {"ok": False}
        self.assertEqual(self.choose().agent, "flash")

    def test_explicit_issue_override_still_respects_restrictions(self):
        self.assertEqual(self.choose("Agent: flash").agent, "flash")
        self.pool.specs["flash"].levels = ["easy"]
        self.assertIsNone(self.choose("Agent: flash"))

    def test_partial_policy_preserves_preference_and_reloads_limits(self):
        running_spec = self.pool.specs["flash"]
        request = {"agents": [{"name": "flash", "roles": ["worker"], "levels": ["easy"]}]}
        self.dashboard.set_routing_policy(request)
        request["agents"][0]["levels"].append("hard")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["routing"]["preferredKindsByLevel"], self.routing["preferredKindsByLevel"])
        reloaded = specs_from(saved["agents"])[0]
        self.assertEqual(reloaded.roles, ["worker"])
        self.assertEqual(reloaded.levels, ["easy"])
        self.assertEqual(self.pool.specs["flash"].levels, ["easy"])
        self.assertIs(self.pool.specs["flash"], running_spec)
        self.dashboard.set_routing_policy({"preferredKindsByLevel": {}})
        self.assertEqual(self.pool.routing["preferredKindsByLevel"], {})

    def test_explicit_empty_limits_disable_eligibility_and_null_clears_limits(self):
        self.dashboard.set_routing_policy({"agents": [{"name": "codex", "roles": []},
                                                       {"name": "claude", "levels": []}]})
        self.assertEqual(self.choose().agent, "flash")
        self.dashboard.set_routing_policy({"agents": [{"name": "codex", "roles": None}]})
        self.assertEqual(self.choose().agent, "codex")

    def test_invalid_policy_never_mutates_live_or_saved_state(self):
        bad_requests = [[], {"agents": [{"name": []}]}, {"agents": [{"name": "flash", "roles": "worker"}]},
                        {"preferredKindsByLevel": {"medium": ["bogus"]}},
                        {"agents": [{"name": "flash", "levels": ["easy"]}, {"name": "codex", "levels": ["bogus"]}]}]
        for request in bad_requests:
            with self.subTest(request=request):
                before = (copy.deepcopy(self.pool.specs), copy.deepcopy(self.pool.routing), self.cfg.dump(), self.path.read_bytes())
                with self.assertRaises(ValueError):
                    self.dashboard.set_routing_policy(request)
                self.assertEqual(before, (self.pool.specs, self.pool.routing, self.cfg.dump(), self.path.read_bytes()))

    def test_disk_failure_rolls_back_config_and_never_publishes_policy(self):
        before = (copy.deepcopy(self.pool.specs), copy.deepcopy(self.pool.routing), self.cfg.dump(), self.path.read_bytes())
        with patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.dashboard.set_routing_policy({"agents": [{"name": "flash", "levels": ["easy"]}]})
        self.assertEqual(before, (self.pool.specs, self.pool.routing, self.cfg.dump(), self.path.read_bytes()))

    def test_added_replacement_model_keeps_supplied_restrictions(self):
        self.dashboard.remove_agent({"name": "flash"})
        with patch("clis.opencode_models", return_value=[]):
            self.dashboard.add_agent({"name": "flash", "kind": "opencode", "model": "opencode/new-flash", "tier": 1,
                                      "roles": ["worker"], "levels": ["easy"]})
        saved = json.loads(self.path.read_text())
        replacement = next(s for s in specs_from(saved["agents"]) if s.name == "flash")
        self.assertEqual(replacement.model, "opencode/new-flash")
        self.assertEqual(replacement.roles, ["worker"])
        self.assertEqual(replacement.levels, ["easy"])
        self.assertFalse(self.pool.specs["flash"].can("merger"))

    def test_add_rejects_invalid_limits_before_inserting_agent(self):
        before = (copy.deepcopy(self.pool.specs), self.cfg.dump(), self.path.read_bytes())
        with self.assertRaises(ValueError):
            self.dashboard.add_agent({"name": "new-codex", "kind": "codex", "tier": 3, "levels": "easy"})
        self.assertEqual(before, (self.pool.specs, self.cfg.dump(), self.path.read_bytes()))

    def test_bad_persisted_restrictions_and_duplicate_names_fail_closed(self):
        for raw in ([{"name": "bad", "roles": "worker"}], [{"name": "bad", "levels": ["mediumish"]}],
                    [{"name": "flash", "levels": ["easy"]}, {"name": "flash"}]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                specs_from(raw)


if __name__ == "__main__":
    unittest.main()
