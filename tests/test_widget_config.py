from __future__ import annotations

import configparser
import re
import unittest
from pathlib import Path


class WidgetConfigTests(unittest.TestCase):
    def test_ini_template_contains_double_click_launch_settings(self) -> None:
        template = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "CodexMonitorWidget.ini.example"
        )
        config = configparser.ConfigParser()

        with template.open("r", encoding="ascii") as handle:
            config.read_file(handle)

        self.assertEqual(
            config["CodexMonitorWidget"]["ApiUrl"],
            "https://codex-monitor.aiof.top/api/sessions",
        )
        self.assertEqual(config["CodexMonitorWidget"]["ApiToken"], "")

    def test_widget_requires_consecutive_empty_responses_before_clearing(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "src"
            / "main.c"
        ).read_text(encoding="utf-8")
        confirmations = re.search(
            r"#define EMPTY_RESULT_CONFIRMATIONS (\d+)",
            source,
        )
        self.assertIsNotNone(confirmations)
        self.assertGreaterEqual(int(confirmations.group(1)), 3)

        fetch_done = source[source.index("case WM_FETCH_DONE:") :]
        fetch_done = fetch_done[: fetch_done.index("case WM_PAINT:")]
        self.assertIn("g_app.empty_success_count++;", fetch_done)
        self.assertIn(
            "g_app.empty_success_count >= EMPTY_RESULT_CONFIRMATIONS",
            fetch_done,
        )
        error_branch_start = fetch_done.index(
            "} else {",
            fetch_done.index("g_app.last_error[0]"),
        )
        error_branch = fetch_done[error_branch_start:]
        self.assertIn("g_app.empty_success_count = 0;", error_branch)


if __name__ == "__main__":
    unittest.main()
