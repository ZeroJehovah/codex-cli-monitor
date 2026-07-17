from __future__ import annotations

import re
import unittest
from pathlib import Path


class WidgetServerColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "src"
            / "main.c"
        ).read_text(encoding="utf-8")

    def test_palette_supports_high_contrast_neighbors(self) -> None:
        palette_match = re.search(
            r"SERVER_COLORS\[SERVER_COLOR_COUNT\]\s*=\s*\{(?P<body>.*?)\};",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(palette_match)
        colors = [
            tuple(map(int, values))
            for values in re.findall(
                r"RGB\((\d+),\s*(\d+),\s*(\d+)\)",
                palette_match.group("body"),
            )
        ]
        threshold_match = re.search(
            r"#define SERVER_COLOR_MIN_DISTANCE_SQUARED \((\d+) \* (\d+)\)",
            self.source,
        )
        self.assertIsNotNone(threshold_match)
        threshold = int(threshold_match.group(1)) * int(threshold_match.group(2))
        self.assertGreaterEqual(threshold, 240 * 240)

        def distance_squared(left: tuple[int, ...], right: tuple[int, ...]) -> int:
            return sum((left_value - right_value) ** 2 for left_value, right_value in zip(left, right))

        self.assertTrue(
            any(
                distance_squared(left, right) < threshold
                for index, left in enumerate(colors)
                for right in colors[index + 1 :]
            ),
            "the palette should be allowed to retain similar non-neighbor colors",
        )
        old_light_purple = (226, 156, 226)
        old_gold = (238, 176, 43)
        self.assertLess(
            distance_squared(old_light_purple, old_gold),
            threshold,
            "the previously reported light-purple and gold pair must be rejected",
        )
        for index, color in enumerate(colors):
            self.assertTrue(
                any(
                    other_index != index and distance_squared(color, other) >= threshold
                    for other_index, other in enumerate(colors)
                ),
                f"palette color {index} needs at least one valid neighboring color",
            )

    def test_palette_colors_remain_visible_on_dark_rows(self) -> None:
        palette_match = re.search(
            r"SERVER_COLORS\[SERVER_COLOR_COUNT\]\s*=\s*\{(?P<body>.*?)\};",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(palette_match)
        colors = [
            tuple(map(int, values))
            for values in re.findall(
                r"RGB\((\d+),\s*(\d+),\s*(\d+)\)",
                palette_match.group("body"),
            )
        ]

        def relative_luminance(color: tuple[int, ...]) -> float:
            channels = []
            for value in color:
                channel = value / 255
                channels.append(
                    channel / 12.92
                    if channel <= 0.04045
                    else ((channel + 0.055) / 1.055) ** 2.4
                )
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

        row_luminance = relative_luminance((34, 34, 34))
        for index, color in enumerate(colors):
            contrast = (relative_luminance(color) + 0.05) / (row_luminance + 0.05)
            self.assertGreaterEqual(
                contrast,
                3.0,
                f"palette color {index} is too dim for a thin bar on the dark row",
            )

    def test_color_reconciliation_runs_after_server_sorting(self) -> None:
        rebuild_match = re.search(
            r"static void rebuild_directory_rows\(void\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(rebuild_match)
        rebuild_body = rebuild_match.group("body")
        self.assertLess(
            rebuild_body.index("sort_directory_rows();"),
            rebuild_body.index("sync_server_colors();"),
        )
        self.assertIn(
            "server_colors_have_high_contrast(previous_color, color_index)",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
