from __future__ import annotations

import re
import unittest
from pathlib import Path


class WidgetTextLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "src"
            / "main.c"
        ).read_text(encoding="utf-8")

    def test_latin_text_keeps_lowercase_o_visual_reference(self) -> None:
        glyph_metrics = re.search(
            r"static int glyph_vertical_metrics\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(glyph_metrics)
        self.assertIn("GetGlyphOutlineW(hdc, L'o'", glyph_metrics.group(0))

    def test_cjk_text_uses_rendered_ink_bounds(self) -> None:
        dispatcher = re.search(
            r"static void text_vertical_metrics\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(dispatcher)
        dispatcher_body = dispatcher.group(0)
        self.assertIn("text_contains_cjk(text, length)", dispatcher_body)
        self.assertIn(
            "rendered_text_vertical_metrics(hdc, text, length, metrics)",
            dispatcher_body,
        )
        self.assertIn("glyph_vertical_metrics(hdc, metrics);", dispatcher_body)

        rendered_metrics = re.search(
            r"static int rendered_text_vertical_metrics\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(rendered_metrics)
        rendered_body = rendered_metrics.group(0)
        self.assertIn("CreateDIBSection", rendered_body)
        self.assertIn("ExtTextOutW", rendered_body)
        self.assertIn("bottom - top + 1", rendered_body)
        self.assertIn("baseline_y - top", rendered_body)

    def test_vertical_metrics_are_cached_outside_animation_paints(self) -> None:
        width_update = re.search(
            r"static void update_directory_column_width\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(width_update)
        update_body = width_update.group(0)
        self.assertIn("&g_app.empty_text_vertical_metrics", update_body)
        self.assertIn("&g_app.rows[row].text_vertical_metrics", update_body)

        draw_text = re.search(
            r"static void draw_directory_text\(.*?\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(draw_text)
        self.assertIn("metrics = *cached_metrics;", draw_text.group(0))


if __name__ == "__main__":
    unittest.main()
