from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener


COLLECTOR_SNAPSHOT_PATH = "/api/collector/snapshot"
FAILURE_LOG_REPEAT_SECONDS = 30.0


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
        opener: OpenerDirector | None = None,
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
        self._opener = opener or build_opener(ProxyHandler({}))
        self.proxy_bypassed = opener is None
        self._status_lock = threading.Lock()
        self.attempt_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.consecutive_failures = 0
        self.last_attempt_at: float | None = None
        self.last_error: str | None = None
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None

    def post_once(self) -> None:
        attempted_at = time.time()
        self._record_attempt(attempted_at)
        try:
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
            with self._opener.open(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"aggregator returned HTTP {response.status}")
                response.read()
        except HTTPError as error:
            message = f"aggregator returned HTTP {error.code}"
            detail = _http_error_detail(error)
            if detail:
                message = f"{message}: {detail}"
            self._record_failure(message, time.time())
            raise RuntimeError(message) from error
        except URLError as error:
            message = f"aggregator connection failed: {error.reason}"
            self._record_failure(message, time.time())
            raise RuntimeError(message) from error
        except Exception as error:
            message = str(error) or type(error).__name__
            self._record_failure(message, time.time())
            raise
        self._record_success(time.time())

    def status_snapshot(self) -> dict:
        with self._status_lock:
            return {
                "url": _safe_status_url(self.url),
                "proxy_bypassed": self.proxy_bypassed,
                "healthy": self.last_success_at is not None
                and self.consecutive_failures == 0,
                "attempt_count": self.attempt_count,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "consecutive_failures": self.consecutive_failures,
                "last_attempt_at": self.last_attempt_at,
                "last_attempt_at_iso": _timestamp_iso(self.last_attempt_at),
                "last_success_at": self.last_success_at,
                "last_success_at_iso": _timestamp_iso(self.last_success_at),
                "last_failure_at": self.last_failure_at,
                "last_failure_at_iso": _timestamp_iso(self.last_failure_at),
                "last_error": self.last_error,
            }

    def run(self, stop_event: threading.Event) -> None:
        last_failure_log_at: float | None = None
        last_logged_error: str | None = None
        ready_logged = False
        _log("INFO", f"collector push started url={self.url} proxy=disabled")
        while not stop_event.is_set():
            started = time.monotonic()
            failures_before_attempt = self.status_snapshot()["consecutive_failures"]
            try:
                self.post_once()
            except Exception as error:  # pragma: no cover - long-running boundary
                status = self.status_snapshot()
                now = time.time()
                if (
                    status["consecutive_failures"] == 1
                    or last_failure_log_at is None
                    or str(error) != last_logged_error
                    or now - last_failure_log_at >= FAILURE_LOG_REPEAT_SECONDS
                ):
                    _log(
                        "ERROR",
                        "collector push failed "
                        f"consecutive={status['consecutive_failures']} "
                        f"total_failures={status['failure_count']} error={error}",
                    )
                    last_failure_log_at = now
                    last_logged_error = str(error)
            else:
                status = self.status_snapshot()
                if failures_before_attempt:
                    _log(
                        "INFO",
                        "collector push recovered "
                        f"after={failures_before_attempt} "
                        f"total_successes={status['success_count']}",
                    )
                elif not ready_logged:
                    _log(
                        "INFO",
                        f"collector push ready total_successes={status['success_count']}",
                    )
                ready_logged = True
                last_failure_log_at = None
                last_logged_error = None
            remaining = self.interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                stop_event.wait(remaining)

    def _record_attempt(self, attempted_at: float) -> None:
        with self._status_lock:
            self.attempt_count += 1
            self.last_attempt_at = attempted_at

    def _record_success(self, succeeded_at: float) -> None:
        with self._status_lock:
            self.success_count += 1
            self.consecutive_failures = 0
            self.last_error = None
            self.last_success_at = succeeded_at

    def _record_failure(self, message: str, failed_at: float) -> None:
        with self._status_lock:
            self.failure_count += 1
            self.consecutive_failures += 1
            self.last_error = message
            self.last_failure_at = failed_at


def _http_error_detail(error: HTTPError) -> str | None:
    try:
        detail = error.read(512).decode("utf-8", errors="replace")
    except OSError:
        return None
    normalized = " ".join(detail.split())
    return normalized[:256] or None


def _safe_status_url(value: str) -> str:
    parsed = urlparse(value)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(parsed._replace(netloc=netloc, query="", fragment=""))


def _timestamp_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _log(level: str, message: str) -> None:
    print(
        f"{_timestamp_iso(time.time())} {level} {message}",
        file=sys.stderr,
        flush=True,
    )
