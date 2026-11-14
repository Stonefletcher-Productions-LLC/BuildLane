"""clis.find_codex follows the Codex app's own CLI so an app update can't leave Fletchery on a stale one (2026-11-14)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clis  # noqa: E402


def touch(p: Path) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return str(p)


class FindCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {"CODEX_HOME": str(self.root / "home"), "LOCALAPPDATA": str(self.root / "local"),
                    "ProgramFiles": str(self.root / "pf"), "ProgramW6432": str(self.root / "pf"), "PATH": ""}
        self.stale = touch(self.root / "local/OpenAI/Codex/bin/5449c1f0f26f1f98/codex.exe")

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, explicit=""):
        with patch.dict(os.environ, self.env, clear=True), patch("shutil.which", return_value=None):
            return clis.find_codex(explicit)

    def test_old_behaviour_without_the_app(self):
        self.assertEqual(self.find(), self.stale)
        self.assertEqual(self.find("C:/x/codex.exe"), "C:/x/codex.exe")

    def test_newest_app_package_beats_the_stale_bin_copy(self):
        touch(self.root / "pf/WindowsApps/OpenAI.Codex_26.1009.1.0_x64__2p2nqsd0c76g0/app/resources/codex.exe")
        new = touch(self.root / "pf/WindowsApps/OpenAI.Codex_26.1106.6628.0_x64__2p2nqsd0c76g0/app/resources/codex.exe")
        self.assertEqual(self.find(), new)

    def test_config_toml_cli_path_wins(self):
        cli = touch(self.root / "app/resources/codex.exe")
        home = self.root / "home"
        home.mkdir()
        (home / "config.toml").write_text('[windows]\nsandbox = "elevated"\n\n[shell_environment_policy.set]\n'
                                          f'CODEX_CLI_PATH = {cli!r}\n'.replace("'", '"').replace("\\", "\\\\"),
                                          encoding="utf-8")
        self.assertEqual(self.find(), cli)

    def test_config_pointing_at_a_missing_file_is_ignored(self):
        home = self.root / "home"
        home.mkdir()
        (home / "config.toml").write_text('CODEX_CLI_PATH = "Z:/gone/codex.exe"\n', encoding="utf-8")
        self.assertEqual(self.find(), self.stale)

    def test_environment_override(self):
        cli = touch(self.root / "env/codex.exe")
        self.env["CODEX_CLI_PATH"] = cli
        self.assertEqual(self.find(), cli)


class HostSessionEnvTests(unittest.TestCase):
    """2026-11-12..14: the backend was started from a Codex Desktop session and handed CODEX_WINDOWS_REGISTERED_CORE=1
    to every Codex worker, whose commands then all failed with "registered Core launch requires an installed app"."""
    POLLUTED = {"PATH": "C:/bin", "CODEX_WINDOWS_REGISTERED_CORE": "1",
                "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "OpenAI.Codex_2p2nqsd0c76g0", "CODEX_SHELL": "1",
                "CODEX_THREAD_ID": "t", "codex_ci": "1", "CODEX_APP_TOOLS_PIPE_PATH": "p", "BROWSER_USE_ACCESS_TOKEN": "x",
                "CODEX_HOME": "C:/h", "CODEX_API_KEY": "k", "CODEX_CLI_PATH": "C:/c.exe", "GH_TOKEN": "g"}

    def test_scrub_keeps_fletcherys_own_settings(self):
        self.assertEqual(clis.scrub_host_session_env(self.POLLUTED),
                         {"PATH": "C:/bin", "CODEX_HOME": "C:/h", "CODEX_API_KEY": "k", "CODEX_CLI_PATH": "C:/c.exe",
                          "GH_TOKEN": "g"})

    def test_scrub_process_env(self):
        with patch.dict(os.environ, self.POLLUTED, clear=True):
            gone = clis.scrub_process_env()
            self.assertNotIn("CODEX_WINDOWS_REGISTERED_CORE", os.environ)
            self.assertIn("CODEX_HOME", os.environ)
        self.assertIn("CODEX_WINDOWS_REGISTERED_CORE", [g.upper() for g in gone])


if __name__ == "__main__":
    unittest.main()
