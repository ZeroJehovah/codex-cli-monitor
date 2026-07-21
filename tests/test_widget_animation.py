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

    def test_widget_animation_uses_high_refresh_timer(self) -> None:
        interval_match = re.search(
            r"#define ANIMATION_FRAME_INTERVAL_MS (\d+)",
            self.source,
        )
        self.assertIsNotNone(interval_match)
        self.assertLessEqual(int(interval_match.group(1)), 8)
        self.assertIn("CreateTimerQueueTimer", self.source)
        self.assertIn("timeBeginPeriod(1)", self.source)
        self.assertIn("QueryPerformanceCounter", self.source)

    def test_high_refresh_timer_covers_visible_animations(self) -> None:
        update_match = re.search(
            r"static void update_animation_timer\(void\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(update_match)
        update_body = update_match.group("body")
        self.assertIn("edge_tuck_animating() || has_running_sessions()", update_body)
        self.assertIn("empty_state_is_connecting()", update_body)
        self.assertIn("stop_animation_frame_timer(0);", update_body)
        self.assertIn("high_refresh_needed && !high_refresh_started", update_body)

    def test_running_pulse_uses_high_resolution_clock(self) -> None:
        pulse_match = re.search(
            r"static int running_pulse_level\(void\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(pulse_match)
        pulse_body = pulse_match.group("body")
        self.assertIn("QueryPerformanceCounter", pulse_body)
        self.assertNotIn("GetTickCount", pulse_body)

    def test_running_glow_uses_one_core_and_continuous_falloff(self) -> None:
        glow_match = re.search(
            r"static void fill_indicator_glow\((?P<body>.*?)\n\}\n\n"
            r"static void draw_status_indicator",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(glow_match)
        glow_body = glow_match.group("body")
        self.assertIn("const RECT *core_rect", glow_body)
        self.assertIn("int spread", glow_body)
        self.assertIn("fade = fade * fade * (3.0 - 2.0 * fade);", glow_body)
        self.assertIn("fade *= fade;", glow_body)

        running_match = re.search(
            r"static void draw_status_indicator\(.*?"
            r"if \(is_running_status\(status\)\) \{(?P<body>.*?)"
            r"\n\s*return;\n\s*\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(running_match)
        running_body = running_match.group("body")
        self.assertEqual(running_body.count("fill_soft_indicator("), 1)
        self.assertNotIn("centered_rect", running_body)


if __name__ == "__main__":
    unittest.main()
