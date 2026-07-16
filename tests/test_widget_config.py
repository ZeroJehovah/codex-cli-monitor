from __future__ import annotations

import configparser
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
            "http://localhost:8765/api/sessions",
        )
        self.assertEqual(config["CodexMonitorWidget"]["ApiToken"], "")


if __name__ == "__main__":
    unittest.main()
