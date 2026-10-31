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

        apply_result = source[source.index(
            "static void apply_successful_fetch_result"
        ) :]
        apply_result = apply_result[: apply_result.index("static int start_websocket_connection")]
        self.assertIn("g_app.empty_success_count++;", apply_result)
        self.assertIn(
            "g_app.empty_success_count >= EMPTY_RESULT_CONFIRMATIONS",
            apply_result,
        )

        fetch_done = source[source.index("case WM_FETCH_DONE:") :]
        fetch_done = fetch_done[: fetch_done.index("case WM_PAINT:")]
        self.assertIn(
            "g_app.empty_success_count = 0;\n"
            "                copy_ascii(g_app.last_error",
            fetch_done,
        )

    def test_widget_confirms_stale_rows_after_websocket_reports_empty(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "windows"
            / "CodexMonitorWidget"
            / "src"
            / "main.c"
        ).read_text(encoding="utf-8")

        timer_body = source[source.index("case WM_TIMER:") :]
        timer_body = timer_body[: timer_body.index("case WM_ANIMATION_FRAME:")]
        self.assertRegex(
            timer_body,
            r"g_app\.session_count > 0 &&\s+g_app\.empty_success_count > 0",
        )
        self.assertIn("REST_RECONCILE_INTERVAL_MS", timer_body)
        self.assertIn('start_websocket_connection(hwnd, "max_age")', timer_body)

        fetch_done = source[source.index("case WM_FETCH_DONE:") :]
        fetch_done = fetch_done[: fetch_done.index("case WM_PAINT:")]
        self.assertIn(
            "result->fetch_generation == g_app.fetch_generation",
            fetch_done,
        )
        self.assertIn("apply_successful_fetch_result(result);", fetch_done)

        connected_body = source[source.index("case WM_WEBSOCKET_CONNECTED:") :]
        connected_body = connected_body[: connected_body.index("case WM_WEBSOCKET_MESSAGE:")]
        self.assertNotIn("KillTimer(hwnd, REFRESH_TIMER_ID);", connected_body)
        self.assertIn("start_fetch();", connected_body)

        closed_body = source[source.index("case WM_WEBSOCKET_CLOSED:") :]
        closed_body = closed_body[: closed_body.index("case WM_PAINT:")]
        self.assertIn(
            "SetTimer(hwnd, REFRESH_TIMER_ID, REFRESH_INTERVAL_MS, NULL);",
            closed_body,
        )

    def test_widget_rejects_messages_from_stale_backend_or_connection(self) -> None:
        root = Path(__file__).parents[1] / "windows" / "CodexMonitorWidget"
        source = (root / "src" / "main.c").read_text(encoding="utf-8")
        websocket_header = (root / "src" / "websocket.h").read_text(encoding="utf-8")
        websocket_source = (root / "src" / "websocket.c").read_text(encoding="utf-8")

        self.assertIn("LONG connection_generation", websocket_header)
        self.assertIn("current_connection_generation", websocket_source)
        self.assertIn("WinHttpWebSocketShutdown", websocket_source)
        self.assertIn('start_websocket_connection(hwnd, "connect_timeout")', source)
        self.assertIn("http_instance_missing_ignored", source)

        message_body = source[source.index("case WM_WEBSOCKET_MESSAGE:") :]
        message_body = message_body[: message_body.index("case WM_WEBSOCKET_CLOSED:")]
        self.assertIn("generation != g_app.active_websocket_generation", message_body)
        self.assertIn("g_app.authoritative_instance_id", message_body)
        self.assertIn('"ws_instance_mismatch"', message_body)


if __name__ == "__main__":
    unittest.main()
