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
        self.assertLess(
            distance_squared(colors[0], colors[5]),
            threshold,
            "the light gray and pale gold shown in the reported case must not be neighbors",
        )
        for index, color in enumerate(colors):
            self.assertTrue(
                any(
                    other_index != index and distance_squared(color, other) >= threshold
                    for other_index, other in enumerate(colors)
                ),
                f"palette color {index} needs at least one valid neighboring color",
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
