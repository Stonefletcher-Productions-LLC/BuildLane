"""Private Blender bridges: started with plain python, owned per folder, only ever stopped by recorded id.
A small Python listener stands in for Blender; no Blender, network or agents."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import blender_bridge as bb
from runtime import role_prompt

LISTENER = ("import socket,sys,time\n"
            "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(5)\n"
            "time.sleep(120)\n")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        # cleanups run last-in first-out: stop the stand-ins (they hold their log files open on Windows), then remove
        # the folder, then forget the registry location
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["FLETCHERY_BRIDGE_DIR"] = str(Path(self.tmp.name) / "reg")
        self.addCleanup(os.environ.pop, "FLETCHERY_BRIDGE_DIR", None)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._stop_all)
        self.script = Path(self.tmp.name) / "listener.py"
        self.script.write_text(LISTENER, encoding="utf-8")
        self.owner = Path(self.tmp.name) / "930-task"
        self.owner.mkdir()

    def _stop_all(self):
        for b in bb.bridges():
            bb.stop(b["port"])
        time.sleep(0.2)

    def _start(self, owner=None, port=0, command=None):
        owner = str(owner or self.owner)
        if command is None:
            # the port is only known once start() picks it, so the stand-in reads it from the registry-free path:
            port = port or next(p for p in bb.PORTS if bb._bindable(p))
            command = [sys.executable, str(self.script), str(port)]
        return bb.start(port=port, owner=owner, command=command, timeout=30)

    def test_start_listens_records_owner_and_is_reused(self):
        first = self._start()
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["started"])
        self.assertTrue(bb.listening(first["port"]))
        self.assertIn(first["port"], bb.PORTS)
        again = bb.start(owner=str(self.owner), command=["never-run"])
        self.assertEqual((again["ok"], again["started"], again["port"]), (True, False, first["port"]))
        listed = bb.bridges()
        self.assertEqual([(b["port"], b["listening"]) for b in listed], [(first["port"], True)])

    def test_another_folder_gets_its_own_port_and_cannot_take_a_used_one(self):
        first = self._start()
        other = Path(self.tmp.name) / "955-task"
        other.mkdir()
        refused = bb.start(port=first["port"], owner=str(other), command=["never-run"])
        self.assertFalse(refused["ok"])
        self.assertIn("does not own", refused["error"])
        second = self._start(owner=other)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(second["port"], first["port"])

    def test_stop_only_touches_bridges_it_started(self):
        first = self._start()
        self.assertFalse(bb.stop(first["port"] + 1)["ok"])        # unknown port: left alone
        self.assertTrue(bb.listening(first["port"]))
        done = bb.stop(first["port"])
        self.assertEqual((done["ok"], done["stopped"]), (True, True))
        time.sleep(0.3)
        self.assertFalse(bb.listening(first["port"]))
        self.assertEqual(bb.bridges(), [])

    def test_recycled_process_id_is_never_stopped(self):
        first = self._start()
        data = bb._load()
        data[str(first["port"])]["started_at"] += 1                # as if the id now belonged to another process
        bb._save(data)
        self.assertFalse(bb._terminate(data[str(first["port"])]))
        self.assertTrue(bb.listening(first["port"]))
        data[str(first["port"])]["started_at"] -= 1
        bb._save(data)

    def test_reap_stops_only_bridges_whose_folder_is_gone(self):
        kept = self._start()
        gone_dir = Path(self.tmp.name) / "1045-task"
        gone_dir.mkdir()
        gone = self._start(owner=gone_dir)
        gone_dir.rmdir()
        self.assertEqual(bb.reap()["stopped"], [gone["port"]])
        self.assertTrue(bb.listening(kept["port"]))
        self.assertEqual([b["port"] for b in bb.bridges()], [kept["port"]])

    def test_failed_start_is_a_runtime_failure_with_the_log(self):
        result = bb.start(owner=str(self.owner),
                          command=[sys.executable, "-c", "import sys; print('addon mcp missing'); sys.exit(3)"], timeout=20)
        self.assertFalse(result["ok"])
        self.assertIn("exited with code 3", result["error"])
        self.assertIn("not a permission refusal", result["error"])
        self.assertIn("addon mcp missing", result["log_tail"])
        self.assertEqual(bb.bridges(), [])

    def test_missing_blender_is_reported(self):
        result = bb.start(owner=str(self.owner), blender=str(Path(self.tmp.name) / "no-blender.exe"))
        self.assertFalse(result["ok"])
        self.assertIn("Blender not found", result["error"])


class PromptTests(unittest.TestCase):
    FIELDS = dict(number="1", title="t", body="b", branch="x", base="main", repo="o/r")

    def test_roles_that_run_commands_are_told_the_plain_python_command(self):
        command = f"python {Path(bb.__file__).resolve().as_posix()} start"
        for role in ("worker", "fixer", "merger"):
            text = role_prompt(role, None, **self.FIELDS)
            self.assertIn(command, text, role)
            self.assertNotIn("If no bridge is listening, use\n`E:/BlenderTools/BlenderMCP/Start-BlenderMCP.ps1`", text)
        for role in ("reviewer", "planner", "architect"):
            self.assertNotIn("blender_bridge.py", role_prompt(role, None, **self.FIELDS), role)

    def test_the_command_is_one_the_worker_role_already_allows(self):
        from runtime import TOOLS
        self.assertIn("Bash(python:*)", TOOLS["worker"]["allowed"])
        self.assertIn("Bash(python:*)", TOOLS["fixer"]["allowed"])
        self.assertTrue(bb.worker_note().count("    python ") == 1)


if __name__ == "__main__":
    unittest.main()
