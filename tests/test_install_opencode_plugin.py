from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_cli_monitor.install_opencode_plugin import (
    PLUGIN_MARKER,
    OpenCodePluginError,
    check_plugin,
    install_plugin,
    main,
    plugin_path,
    plugin_source,
    uninstall_plugin,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class PluginAssetTests(unittest.TestCase):
    def test_shipped_asset_is_recognizable_and_body_free(self) -> None:
        payload = plugin_source(REPO_ROOT).read_text(encoding="utf-8")
        self.assertIn(PLUGIN_MARKER, payload)
        # The plugin must subscribe to the observational stream only: taking
        # `permission.ask` would let the monitor answer OpenCode's prompts.
        self.assertNotIn("permission.ask:", payload)
        self.assertNotIn("\"permission.ask\"", payload)
        # Nothing that could carry command text or file bodies is recorded.
        self.assertNotIn("patterns:", payload)
        self.assertNotIn("metadata:", payload)


class InstallPluginTests(unittest.TestCase):
    def test_install_creates_the_plugin_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            first = install_plugin(config_dir, REPO_ROOT)
            destination = plugin_path(config_dir)
            mtime = destination.stat().st_mtime_ns
            second = install_plugin(config_dir, REPO_ROOT)

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(destination.stat().st_mtime_ns, mtime)
            self.assertEqual(
                destination.read_bytes(),
                plugin_source(REPO_ROOT).read_bytes(),
            )
            # OpenCode discovers plugins by globbing this directory, so the file
            # must land there and nowhere else.
            self.assertEqual(
                [item.name for item in (config_dir / "plugin").iterdir()],
                [destination.name],
            )

    def test_install_is_not_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            install_plugin(config_dir, REPO_ROOT)
            mode = stat.S_IMODE(plugin_path(config_dir).stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_install_refreshes_a_stale_monitor_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            destination = plugin_path(config_dir)
            destination.parent.mkdir(parents=True)
            destination.write_text(
                f"// {PLUGIN_MARKER}\n// an older revision\n", encoding="utf-8"
            )
            result = install_plugin(config_dir, REPO_ROOT)

            self.assertTrue(result.changed)
            self.assertEqual(
                destination.read_bytes(),
                plugin_source(REPO_ROOT).read_bytes(),
            )

    def test_install_never_overwrites_a_foreign_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            destination = plugin_path(config_dir)
            destination.parent.mkdir(parents=True)
            original = "export const Mine = async () => ({})\n"
            destination.write_text(original, encoding="utf-8")

            with self.assertRaises(OpenCodePluginError):
                install_plugin(config_dir, REPO_ROOT)
            self.assertEqual(destination.read_text(encoding="utf-8"), original)

    def test_install_rejects_an_unrecognized_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "assets" / "opencode").mkdir(parents=True)
            (root / "assets" / "opencode" / "codex-monitor-decisions.js").write_text(
                "export const Other = 1\n", encoding="utf-8"
            )
            with self.assertRaises(OpenCodePluginError):
                install_plugin(Path(tmp) / "opencode", root)
            self.assertFalse(plugin_path(Path(tmp) / "opencode").exists())

    def test_missing_source_is_reported_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OpenCodePluginError):
                install_plugin(Path(tmp) / "opencode", Path(tmp) / "absent")

    def test_failed_write_leaves_no_partial_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"

            def failing_fdopen(descriptor: int, *args: object, **kwargs: object):
                os.close(descriptor)
                raise OSError("simulated write failure")

            with patch(
                "codex_cli_monitor.install_opencode_plugin.os.fdopen",
                side_effect=failing_fdopen,
            ):
                with self.assertRaises(OSError):
                    install_plugin(config_dir, REPO_ROOT)

            self.assertFalse(plugin_path(config_dir).exists())
            self.assertEqual(list((config_dir / "plugin").iterdir()), [])


class UninstallPluginTests(unittest.TestCase):
    def test_uninstall_removes_only_the_monitor_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            install_plugin(config_dir, REPO_ROOT)
            neighbour = config_dir / "plugin" / "third-party.js"
            neighbour.write_text("export const Third = 1\n", encoding="utf-8")

            result = uninstall_plugin(config_dir)

            self.assertTrue(result.changed)
            self.assertFalse(plugin_path(config_dir).exists())
            self.assertTrue(neighbour.is_file())

    def test_uninstall_without_an_installation_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = uninstall_plugin(Path(tmp) / "opencode")
        self.assertFalse(result.changed)

    def test_uninstall_never_removes_a_foreign_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            destination = plugin_path(config_dir)
            destination.parent.mkdir(parents=True)
            destination.write_text("export const Mine = 1\n", encoding="utf-8")

            with self.assertRaises(OpenCodePluginError):
                uninstall_plugin(config_dir)
            self.assertTrue(destination.is_file())


class CheckPluginTests(unittest.TestCase):
    def test_check_reports_a_current_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            install_plugin(config_dir, REPO_ROOT)
            result = check_plugin(config_dir, REPO_ROOT)

        self.assertTrue(result.installed)
        self.assertTrue(result.current)
        self.assertFalse(result.foreign_file)
        self.assertEqual(result.detail, "current")

    def test_check_reports_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = check_plugin(Path(tmp) / "opencode", REPO_ROOT)
        self.assertFalse(result.installed)
        self.assertFalse(result.current)
        self.assertEqual(result.detail, "not installed")

    def test_check_reports_an_out_of_date_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            destination = plugin_path(config_dir)
            destination.parent.mkdir(parents=True)
            destination.write_text(f"// {PLUGIN_MARKER}\n", encoding="utf-8")
            result = check_plugin(config_dir, REPO_ROOT)

        self.assertTrue(result.installed)
        self.assertFalse(result.current)
        self.assertEqual(result.detail, "out of date")

    def test_check_reports_a_foreign_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            destination = plugin_path(config_dir)
            destination.parent.mkdir(parents=True)
            destination.write_text("export const Mine = 1\n", encoding="utf-8")
            result = check_plugin(config_dir, REPO_ROOT)

        self.assertTrue(result.foreign_file)
        self.assertFalse(result.current)
        self.assertEqual(result.detail, "foreign file present")

    def test_check_cli_has_nonzero_status_when_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(
            StringIO()
        ), redirect_stderr(StringIO()):
            status = main(["--config-dir", tmp, "--repo-root", str(REPO_ROOT), "--check"])
        self.assertEqual(status, 1)

    def test_cli_install_then_check_then_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            arguments = ["--config-dir", tmp, "--repo-root", str(REPO_ROOT)]
            with redirect_stdout(StringIO()) as installed:
                self.assertEqual(main(arguments), 0)
            with redirect_stdout(StringIO()):
                self.assertEqual(main(arguments + ["--check"]), 0)
            with redirect_stdout(StringIO()):
                self.assertEqual(main(arguments + ["--uninstall"]), 0)
            self.assertFalse(plugin_path(Path(tmp)).exists())
        self.assertIn("Restart OpenCode", installed.getvalue())

    def test_cli_reports_a_foreign_file_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = plugin_path(Path(tmp))
            destination.parent.mkdir(parents=True)
            destination.write_text("export const Mine = 1\n", encoding="utf-8")
            errors = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(errors):
                status = main(["--config-dir", tmp, "--repo-root", str(REPO_ROOT)])
            self.assertEqual(destination.read_text(encoding="utf-8"), "export const Mine = 1\n")
        self.assertEqual(status, 2)
        self.assertIn("refusing to overwrite", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
