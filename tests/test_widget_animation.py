from __future__ import annotations

import re
import unittest
from pathlib import Path


class WidgetAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "src"
            / "main.c"
        ).read_text(encoding="utf-8")

    def test_edge_tuck_uses_high_refresh_timer(self) -> None:
        interval_match = re.search(
            r"#define EDGE_TUCK_FRAME_INTERVAL_MS (\d+)",
            self.source,
        )
        self.assertIsNotNone(interval_match)
        self.assertLessEqual(int(interval_match.group(1)), 8)
        self.assertIn("CreateTimerQueueTimer", self.source)
        self.assertIn("timeBeginPeriod(1)", self.source)
        self.assertIn("QueryPerformanceCounter", self.source)

    def test_high_refresh_timer_is_limited_to_tuck_animation(self) -> None:
        update_match = re.search(
            r"static void update_animation_timer\(void\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(update_match)
        update_body = update_match.group("body")
        self.assertIn("if (edge_tuck_animating())", update_body)
        self.assertIn("stop_edge_tuck_timer(0);", update_body)
        self.assertIn("!edge_timer_started", update_body)


if __name__ == "__main__":
    unittest.main()
