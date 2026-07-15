from __future__ import annotations

import json
import sys
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


COLLECTOR_SNAPSHOT_PATH = "/api/collector/snapshot"


def normalize_aggregator_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("aggregator URL must be an absolute HTTP or HTTPS URL")
    if parsed.path in {"", "/"}:
        return urlunparse(parsed._replace(path=COLLECTOR_SNAPSHOT_PATH))
    return value


class CollectorPusher:
    def __init__(
        self,
        url: str,
        token: str,
        snapshot_provider: Callable[[], dict],
        interval_seconds: float = 0.5,
        timeout_seconds: float = 5.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("collector interval must be positive")
        if not token:
            raise ValueError("collector token must not be empty")
        self.url = normalize_aggregator_url(url)
        self.token = token
        self.snapshot_provider = snapshot_provider
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    def post_once(self) -> None:
        body = json.dumps(
            self.snapshot_provider(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "codex-cli-monitor-collector/1",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"aggregator returned HTTP {response.status}")
                response.read()
        except HTTPError as error:
            self.last_error = f"aggregator returned HTTP {error.code}"
            raise RuntimeError(self.last_error) from error
        except URLError as error:
            self.last_error = f"aggregator connection failed: {error.reason}"
            raise RuntimeError(self.last_error) from error
        self.last_error = None
        self.last_success_at = time.time()

    def run(self, stop_event: threading.Event) -> None:
        reported_error: str | None = None
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.post_once()
            except Exception as error:  # pragma: no cover - long-running boundary
                self.last_error = str(error)
                if self.last_error != reported_error:
                    print(f"collector push failed: {self.last_error}", file=sys.stderr)
                    reported_error = self.last_error
            else:
                if reported_error is not None:
                    print("collector push recovered", file=sys.stderr)
                reported_error = None
            remaining = self.interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                stop_event.wait(remaining)
