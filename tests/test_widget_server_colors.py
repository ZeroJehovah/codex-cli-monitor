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

    def test_status_colors_stay_distinguishable_from_server_bars(self) -> None:
        # In the tucked layout the server bar sits one gap away from the status
        # indicator, so the amber added for 待确认 must not read as a server bar.
        palette_match = re.search(
            r"SERVER_COLORS\[SERVER_COLOR_COUNT\]\s*=\s*\{(?P<body>.*?)\};",
            self.source,
            re.DOTALL,
        )
        status_match = re.search(
            r"static COLORREF status_color\(const char \*status\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(palette_match)
        self.assertIsNotNone(status_match)

        def colors(body: str) -> list[tuple[int, ...]]:
            return [
                tuple(map(int, values))
                for values in re.findall(r"RGB\((\d+),\s*(\d+),\s*(\d+)\)", body)
            ]

        palette = colors(palette_match.group("body"))
        # The trailing fallback repeats the success color, so compare distinct ones.
        status_colors = list(dict.fromkeys(colors(status_match.group("body"))))
        self.assertEqual(len(status_colors), 4, "each status needs its own color")

        def distance_squared(left: tuple[int, ...], right: tuple[int, ...]) -> int:
            return sum((one - other) ** 2 for one, other in zip(left, right))

        # 115 is the tightest pair the shipped design already tolerated
        # (magenta server bar against the red failure indicator).
        floor = 115 * 115
        for status in status_colors:
            for index, color in enumerate(palette):
                self.assertGreaterEqual(
                    distance_squared(status, color),
                    floor,
                    f"status {status} is too close to palette color {index}",
                )
        for index, left in enumerate(status_colors):
            for right in status_colors[index + 1 :]:
                self.assertGreaterEqual(
                    distance_squared(left, right),
                    floor,
                    f"status colors {left} and {right} are too close",
                )

    def test_waiting_status_has_its_own_indicator_color(self) -> None:
        self.assertIn(
            'static const char STATUS_WAITING[] = "\\xe5\\xbe\\x85\\xe7\\xa1\\xae\\xe8\\xae\\xa4";',
            self.source,
        )
        status_match = re.search(
            r"static COLORREF status_color\(const char \*status\) \{(?P<body>.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(status_match)
        body = status_match.group("body")
        self.assertIn("is_waiting_status(status)", body)
        # Amber: warm, high red, no blue, so it cannot be confused with the
        # blue running glow even at a glance.
        amber_match = re.search(
            r"if \(is_waiting_status\(status\)\) \{\s*return RGB\((\d+), (\d+), (\d+)\);",
            body,
        )
        self.assertIsNotNone(amber_match)
        red, green, blue = (int(value) for value in amber_match.groups())
        self.assertGreater(red, 200)
        self.assertGreater(red, green)
        self.assertLess(blue, 60)

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
