"""Asset folders the PC shares with nodes (workers.machines.assetRoots): the coordinator lists and serves them, the
worker mirrors them before imports, and a round refuses an import when a folder is missing (Alex, 2026-11-13)."""
import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import machines  # noqa: E402
import node_worker  # noqa: E402
from machines import Machines, WorkerApi  # noqa: E402

SHA = "a" * 40
NODE = ["os:windows", "ram:24", "role:tests", "role:imports", "unreal:5.8.4"]


class FakeCoord:
    """Calls the real worker API handler in-process, as the node would over HTTP."""

    def __init__(self, m, key, host_override=None):
        self.api = WorkerApi(m, "127.0.0.1:0", log=lambda *a: None)
        self.key = key
        self.host_override = host_override
        self.reads = 0

    def call(self, path, body, timeout=30):
        code, obj = self.api.handle(path, "SF-NODE-01", self.key, "100.77.158.113", json.loads(json.dumps(body)))
        if code != 200:
            raise RuntimeError(f"{path}: HTTP {code} {obj.get('error', '')}")
        if path == "/v1/assets/list" and self.host_override is not None:
            obj["host"] = self.host_override
        if path == "/v1/assets/read":
            self.reads += 1
        return obj


class AssetSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(self.tmp.name)
        self.m = Machines(base / "state", "owner/repo")
        self.key = self.m.add_machine("SF-NODE-01")
        self.m.seen("SF-NODE-01", "100.77.158.113", NODE + ["cap:asset-sync"], 24, "2", {})
        self.src = base / "pc" / "meshy_export"
        (self.src / "meshy_folk_v1_r9").mkdir(parents=True)
        (self.src / "meshy_folk_v1_r9" / "Manny.fbx").write_bytes(b"fbx" * 1000)
        (self.src / "big.zip").write_bytes(bytes(range(256)) * 40000)
        (self.src / "empty.txt").write_bytes(b"")
        self.dest = base / "node" / "meshy_export"
        self.m.set_asset_roots([str(self.src)])
        self.cfg = {"assetMap": {str(self.src): str(self.dest)}}

    def tearDown(self):
        self.tmp.cleanup()

    def coord(self):
        return FakeCoord(self.m, self.key, host_override="the-pc")

    def test_mirror_copies_everything_then_only_changes(self):
        c = self.coord()
        got = node_worker.sync_assets(c, self.cfg, log=lambda *a: None)
        self.assertEqual(got["copied"], 3)
        for rel in ("meshy_folk_v1_r9/Manny.fbx", "big.zip", "empty.txt"):
            self.assertEqual((self.dest / rel).read_bytes(), (self.src / rel).read_bytes())
        self.assertFalse(list(self.dest.rglob("*.fletchery-part")))
        again = node_worker.sync_assets(c, self.cfg, log=lambda *a: None)
        self.assertEqual(again["copied"], 0)
        (self.src / "meshy_folk_v1_r9" / "Manny.fbx").write_bytes(b"new" * 999)
        third = node_worker.sync_assets(c, self.cfg, log=lambda *a: None)
        self.assertEqual(third["copied"], 1)
        self.assertEqual((self.dest / "meshy_folk_v1_r9" / "Manny.fbx").read_bytes(), b"new" * 999)

    def test_a_long_mirror_keeps_checking_in(self):
        for i in range(5):
            (self.src / f"part{i}.bin").write_bytes(b"p" * 100)
        beats = []
        clock = iter(range(0, 10**6, 20))   # each look at the clock is 20 s later
        with mock.patch.object(node_worker.time, "monotonic", lambda: next(clock)):
            node_worker.sync_assets(self.coord(), self.cfg, log=lambda *a: None, beat=lambda: beats.append(1))
        self.assertGreaterEqual(len(beats), 2)

    def test_big_file_comes_in_chunks(self):
        (self.src / "big.zip").write_bytes(b"x" * (machines.ASSET_CHUNK * 2 + 5))
        c = self.coord()
        node_worker.sync_assets(c, self.cfg, log=lambda *a: None)
        self.assertEqual((self.dest / "big.zip").stat().st_size, machines.ASSET_CHUNK * 2 + 5)

    def test_bookkeeping_is_not_shared_back(self):
        node_worker.sync_assets(self.coord(), self.cfg, log=lambda *a: None)
        (self.src / node_worker.ASSET_INDEX).write_text("{}")
        names = [f[0] for r in self.m.asset_list()["roots"] for f in r["files"]]
        self.assertNotIn(node_worker.ASSET_INDEX, names)

    def test_worker_on_the_pc_itself_copies_nothing(self):
        c = FakeCoord(self.m, self.key, host_override=None)   # the real host name: same machine
        got = node_worker.sync_assets(c, {}, log=lambda *a: None)
        self.assertEqual(got["copied"], 0)

    def test_missing_folder_stops_the_sync(self):
        self.m.set_asset_roots([str(self.src), str(self.src.parent / "gone")])
        with self.assertRaises(node_worker.AssetSyncError) as e:
            node_worker.sync_assets(self.coord(), self.cfg, log=lambda *a: None)
        self.assertIn("gone", str(e.exception))

    def test_read_refuses_paths_outside_the_shared_folder(self):
        (self.src.parent / "secret.txt").write_text("no")
        api = WorkerApi(self.m, "127.0.0.1:0", log=lambda *a: None)
        for rel in ("../secret.txt", str(self.src.parent / "secret.txt"), ""):
            code, _ = api.handle("/v1/assets/read", "SF-NODE-01", self.key, "x", {"root": str(self.src), "path": rel})
            self.assertEqual(code, 400, rel)
        code, _ = api.handle("/v1/assets/read", "SF-NODE-01", self.key, "x", {"root": str(self.src.parent), "path": "secret.txt"})
        self.assertEqual(code, 409)
        code, _ = api.handle("/v1/assets/list", "SF-NODE-01", "wrong", "y", {})
        self.assertEqual(code, 401)

    def test_unsafe_path_from_the_pc_is_refused(self):
        class Evil(FakeCoord):
            def call(self, path, body, timeout=30):
                return {"host": "pc", "roots": [{"root": "E:/x", "files": [["../../Windows/x.dll", 1, "0" * 64]]}], "missing": []}
        with self.assertRaises(node_worker.AssetSyncError):
            node_worker.sync_assets(Evil(self.m, self.key), {"assetMap": {"E:/x": str(self.dest)}}, log=lambda *a: None)

    def test_old_coordinator_means_nothing_to_mirror(self):
        class Old(FakeCoord):
            def call(self, path, body, timeout=30):
                raise RuntimeError(f"{path}: HTTP 404 not found")
        self.assertEqual(node_worker.sync_assets(Old(self.m, self.key), {}, log=lambda *a: None)["copied"], 0)


class RoundGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.m = Machines(self.tmp.name, "owner/repo")
        self.root = Path(self.tmp.name) / "meshy_export"
        (self.root / "r9").mkdir(parents=True)
        (self.root / "r9" / "a.fbx").write_text("x")
        self.m.set_asset_roots([str(self.root)])
        self.params = {"only": "RunSpecs", "imports": "ImportMeshyFolk", "push_branch": "fletchery/round-9"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_quick(self):
        out = io.StringIO()
        code = machines.run_job(self.m, "node-checks", SHA, dict(self.params), labels=["role:imports"],
                                out=lambda s: out.write(s + "\n"), clock=lambda: 10**9, sleep=lambda s: None)
        return code, out.getvalue()

    def test_missing_folder_stops_an_import_before_it_is_queued(self):
        (self.root / "r9" / "a.fbx").unlink()
        (self.root / "r9").rmdir()
        code, text = self.run_quick()
        self.assertEqual(code, 2)
        self.assertIn("missing or empty on this PC", text)
        self.assertEqual(self.m.view().get("jobs", []) if isinstance(self.m.view(), dict) else [], [])

    def test_an_old_worker_cannot_take_imports(self):
        self.m.add_machine("SF-NODE-01")
        self.m.seen("SF-NODE-01", "1.2.3.4", NODE, 24, "1", {})   # no cap:asset-sync
        code, text = self.run_quick()
        self.assertEqual(code, 2)
        self.assertIn("cap:asset-sync", text)

    def test_hello_says_the_worker_mirrors(self):
        seen = {}

        class C:
            def call(self, path, body, timeout=30):
                seen.update(body)
                return {}
        node_worker.hello(C(), {"labels": ["os:windows"]})
        self.assertIn("cap:asset-sync", seen["labels"])

    def test_profile_roots_reach_the_command_line(self):
        again = Machines(self.tmp.name, "owner/repo")
        self.assertEqual(again.asset_roots(), [str(self.root)])


if __name__ == "__main__":
    unittest.main()
