from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


LAUNCHER = Path(__file__).resolve().parents[1] / "start-local-collector.sh"


class LocalCollectorLauncherTests(unittest.TestCase):
    def test_restart_retains_configuration_and_keeps_token_out_of_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "collector.env"
            config.write_text(
                "CODEX_MONITOR_SERVER_ID=test-local\n"
                "CODEX_MONITOR_SERVER_NAME='Test Local'\n"
                "CODEX_MONITOR_AGGREGATOR_URL=https://monitor.invalid\n"
                "CODEX_MONITOR_COLLECTOR_TOKEN=fixture-secret\n"
                "CODEX_MONITOR_LOCAL_PORT=8766\n"
                "CODEX_MONITOR_COLLECTOR_INTERVAL=0.75\n",
                encoding="utf-8",
            )
            record = root / "calls.jsonl"
            interpreter = root / "python-fixture"
            interpreter.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "with open(os.environ['COLLECTOR_TEST_RECORD'], 'a') as handle:\n"
                "    handle.write(json.dumps({'args': sys.argv[1:], "
                "'token': os.environ.get('CODEX_MONITOR_COLLECTOR_TOKEN')}) + '\\n')\n",
                encoding="utf-8",
            )
            interpreter.chmod(0o700)
            env = self._environment(config)
            env.update({
                "CODEX_MONITOR_PYTHON": str(interpreter),
                "COLLECTOR_TEST_RECORD": str(record),
                "XDG_STATE_HOME": str(root / "state"),
            })
            result = subprocess.run(
                ["bash", str(LAUNCHER)], env=env, capture_output=True, text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertIn("--stop", calls[0]["args"])
            launch = calls[1]["args"]
            self.assertIn("--daemon", launch)
            self.assertIn("--ws-enabled", launch)
            for flag, expected in (
                ("--server-id", "test-local"),
                ("--server-name", "Test Local"),
                ("--collector-url", "https://monitor.invalid"),
                ("--collector-interval", "0.75"),
                ("--port", "8766"),
            ):
                self.assertEqual(launch[launch.index(flag) + 1], expected)
            self.assertEqual(calls[1]["token"], "fixture-secret")
            self.assertNotIn("fixture-secret", " ".join(launch) + result.stdout + result.stderr)

    def test_missing_credentials_fail_before_stopping_the_existing_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "collector.env"
            config.write_text(
                "CODEX_MONITOR_AGGREGATOR_URL=https://monitor.invalid\n",
                encoding="utf-8",
            )
            env = self._environment(config)
            env["CODEX_MONITOR_PYTHON"] = "/must-not-be-invoked"
            result = subprocess.run(
                ["bash", str(LAUNCHER)], env=env, capture_output=True, text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CODEX_MONITOR_COLLECTOR_TOKEN is required", result.stderr)
            self.assertNotIn("Python interpreter", result.stderr)

    @staticmethod
    def _environment(config: Path) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_MONITOR_")}
        env["CODEX_MONITOR_ENV_FILE"] = str(config)
        return env


if __name__ == "__main__":
    unittest.main()
