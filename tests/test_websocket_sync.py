from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest

from codex_cli_monitor.aggregation import RemoteSnapshot, ServerIdentity
from codex_cli_monitor.api import ApiConfig, ReusableThreadingHTTPServer, make_api_handler
from codex_cli_monitor.websocket_sync import SyncWebSocketClient, WebSocketBroadcaster


def _masked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = b"test"
    encoded = bytearray((0x80 | opcode, 0x80 | len(payload)))
    encoded.extend(mask)
    encoded.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(encoded)


def _read_server_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = sock.recv(2)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    payload = bytearray()
    while len(payload) < length:
        payload.extend(sock.recv(length - len(payload)))
    return opcode, bytes(payload)


class _Provider:
    def get(self):
        return (), time.time()


class WebSocketTests(unittest.TestCase):
    def test_frame_reader_handles_short_socket_reads(self) -> None:
        left, right = socket.socketpair()
        try:
            client = SyncWebSocketClient(left)
            frame = _masked_frame(b'{"token":"secret"}')
            right.sendall(frame[:1])
            right.sendall(frame[1:4])
            right.sendall(frame[4:])
            self.assertEqual(client._read_frame(timeout=1), (0x1, b'{"token":"secret"}'))
        finally:
            left.close()
            right.close()

    def test_broadcast_includes_remote_status_changes(self) -> None:
        class Client:
            closed = False

            def __init__(self) -> None:
                self.messages: list[dict] = []

            def send_text(self, value: str) -> None:
                self.messages.append(json.loads(value))

            def send_control(self, opcode: int, payload: bytes = b"") -> None:
                return None

        client = Client()
        broadcaster = WebSocketBroadcaster()
        broadcaster.register(client)  # type: ignore[arg-type]
        local = ServerIdentity("local", "Local", None)
        remote = ServerIdentity("remote", "Remote", None)
        base = {
            "session_key": "remote:1:1",
            "pid": 1,
            "started_at": 1.0,
            "directory": "/work",
            "cli_type": "codex",
            "waiting_reason": None,
        }
        for status in ("运行中", "成功", "失败", "待确认"):
            snapshot = RemoteSnapshot(
                remote,
                observed_at=1.0,
                received_at=1.0,
                sessions=({**base, "status": status},),
            )
            broadcaster.broadcast((), local, (snapshot,))
        self.assertEqual(
            [item["sessions"][0]["status"] for item in client.messages],
            ["运行中", "成功", "失败", "待确认"],
        )

    def test_register_queues_initial_snapshot_before_future_broadcasts(self) -> None:
        class Client:
            closed = False

            def __init__(self) -> None:
                self.messages: list[str] = []

            def send_text(self, value: str) -> None:
                self.messages.append(value)

        client = Client()
        broadcaster = WebSocketBroadcaster()
        broadcaster.register(client, initial_message="initial")  # type: ignore[arg-type]
        self.assertEqual(client.messages, ["initial"])

    def test_http_upgrade_uses_http11_and_authenticates(self) -> None:
        identity = ServerIdentity("local", "Local", None)
        broadcaster = WebSocketBroadcaster()
        handler = make_api_handler(
            ApiConfig(ws_enabled=True, api_token="secret"),
            identity=identity,
            provider=_Provider(),
            ws_broadcaster=broadcaster,
        )
        server = ReusableThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                key = base64.b64encode(b"test-key").decode()
                client.sendall(
                    (
                        "GET /ws HTTP/1.1\r\n"
                        "Host: localhost\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                    ).encode()
                )
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(client.recv(4096))
                self.assertTrue(response.startswith(b"HTTP/1.1 101"))
                expected_accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                ).decode()
                self.assertIn(
                    f"Sec-WebSocket-Accept: {expected_accept}".encode(),
                    response,
                )
                client.sendall(_masked_frame(b'{"token":"secret"}'))
                opcode, payload = _read_server_frame(client)
                self.assertEqual((opcode, json.loads(payload)), (0x1, {"ok": True}))
                opcode, payload = _read_server_frame(client)
                self.assertEqual(opcode, 0x1)
                self.assertEqual(json.loads(payload)["session_count"], 0)
                client.sendall(_masked_frame(b"", opcode=0x8))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
