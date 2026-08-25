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
        self.assertIn("edge_tuck_animating() || has_animated_sessions()", update_body)
        self.assertIn("empty_state_is_connecting()", update_body)
        self.assertIn("stop_animation_frame_timer(0);", update_body)
        self.assertIn("high_refresh_needed && !high_refresh_started", update_body)

    def test_high_refresh_timer_covers_waiting_sessions(self) -> None:
        # A 待确认 row glows too, so it has to keep the frame timer alive and
        # be painted on the dynamic layer instead of the cached bitmap.
        animated_match = re.search(
            r"static int is_animated_status\(const char \*status\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(animated_match)
        self.assertIn(
            "is_running_status(status) || is_waiting_status(status)",
            animated_match.group("body"),
        )
        sessions_match = re.search(
            r"static int has_animated_sessions\(void\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(sessions_match)
        self.assertIn("is_animated_status(", sessions_match.group("body"))
        static_match = re.search(
            r"static void paint_widget_static\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        dynamic_match = re.search(
            r"static void draw_widget_dynamic\(.*?\n\}\n",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(static_match)
        self.assertIsNotNone(dynamic_match)
        self.assertIn(
            "if (!is_animated_status(g_app.sessions[session_index].status))",
            static_match.group(0),
        )
        self.assertIn(
            "if (is_animated_status(g_app.sessions[session_index].status))",
            dynamic_match.group(0),
        )

    def test_running_pulse_uses_high_resolution_clock(self) -> None:
        pulse_match = re.search(
            r"static int pulse_level\(int period_ms\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(pulse_match)
        pulse_body = pulse_match.group("body")
        self.assertIn("QueryPerformanceCounter", pulse_body)
        self.assertNotIn("GetTickCount", pulse_body)
        self.assertIn(
            "return pulse_level(RUNNING_PULSE_PERIOD_MS);",
            self.source,
        )

    def test_waiting_pulse_breathes_slower_than_the_running_pulse(self) -> None:
        # Hue alone would not survive a colour-blind glance, so the two open-turn
        # states also differ in rhythm.
        running = re.search(r"#define RUNNING_PULSE_PERIOD_MS (\d+)", self.source)
        waiting = re.search(r"#define WAITING_PULSE_PERIOD_MS (\d+)", self.source)
        self.assertIsNotNone(running)
        self.assertIsNotNone(waiting)
        self.assertGreater(int(waiting.group(1)), int(running.group(1)))
        self.assertIn(
            "return pulse_level(WAITING_PULSE_PERIOD_MS);",
            self.source,
        )

    def test_waiting_glow_is_a_single_amber_field(self) -> None:
        waiting_match = re.search(
            r"static void draw_status_indicator\(.*?"
            r"if \(is_waiting_status\(status\)\) \{(?P<body>.*?)"
            r"\n\s*return;\n\s*\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(waiting_match)
        waiting_body = waiting_match.group("body")
        self.assertIn("waiting_pulse_level()", waiting_body)
        self.assertEqual(waiting_body.count("fill_luminous_indicator("), 1)
        self.assertNotIn("fill_soft_indicator(", waiting_body)

    def test_running_glow_uses_one_continuous_luminous_field(self) -> None:
        glow_match = re.search(
            r"static void fill_luminous_indicator_direct\((?P<body>.*?)\n\}\n\n"
            r"static void draw_status_indicator",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(glow_match)
        glow_body = glow_match.group("body")
        self.assertIn("const RECT *rect", glow_body)
        self.assertIn("int spread", glow_body)
        self.assertIn("if (height >= width)", glow_body)
        self.assertIn("segment_start = rect->top + radius", glow_body)
        self.assertIn("segment_start = rect->left + radius", glow_body)
        self.assertIn(
            "(outer_radius_squared - distance_squared) / outer_radius_squared",
            glow_body,
        )
        self.assertIn("fade = fade * fade * (3.0 - 2.0 * fade);", glow_body)
        self.assertIn("fade *= fade;", glow_body)
        self.assertNotIn("core_radius", glow_body)

        running_match = re.search(
            r"static void draw_status_indicator\(.*?"
            r"if \(is_running_status\(status\)\) \{(?P<body>.*?)"
            r"\n\s*return;\n\s*\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(running_match)
        running_body = running_match.group("body")
        self.assertEqual(running_body.count("fill_luminous_indicator("), 1)
        self.assertNotIn("fill_soft_indicator(", running_body)

    def test_widget_caches_indicator_bitmaps_and_reuses_buffers(self) -> None:
        self.assertIn("INDICATOR_BITMAP_CACHE_CAPACITY", self.source)
        self.assertIn("CreateDIBSection", self.source)
        self.assertIn("AlphaBlend", self.source)
        self.assertIn("clear_indicator_bitmap_cache();", self.source)
        buffer_match = re.search(
            r"static void paint_widget_buffered\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(buffer_match)
        buffer_body = buffer_match.group(0)
        self.assertIn("ensure_widget_buffer", buffer_body)
        self.assertNotIn("CreateCompatibleBitmap", buffer_body)
        self.assertNotIn("DeleteObject(bitmap)", buffer_body)


if __name__ == "__main__":
    unittest.main()
