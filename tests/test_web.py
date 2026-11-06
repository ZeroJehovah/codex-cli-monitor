from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
import threading
import unittest

from codex_cli_monitor.api import ApiConfig, ReusableThreadingHTTPServer, make_api_handler
from codex_cli_monitor.aggregation import ServerIdentity


class Provider:
    def get(self):
        return (), 1.0


@contextmanager
def web_server(token="read-secret"):
    server = ReusableThreadingHTTPServer(("127.0.0.1", 0), make_api_handler(
        ApiConfig(api_token=token),
        identity=ServerIdentity("local", "Local", None), provider=Provider(),
    ))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get(address, path, headers=None):
    connection = HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        connection.close()


class WebTests(unittest.TestCase):
    def test_page_requires_exactly_one_valid_query_read_token(self):
        with web_server() as address:
            for path in (
                "/", "/?token=", "/?token=wrong", "/?token=write-secret",
                "/?token=%E4%B8%AD", "/?token=read-secret&token=wrong",
                "/?token=&token=read-secret", "/web/", "/web",
            ):
                with self.subTest(path=path):
                    status, headers, body = get(address, path, {"Authorization": "Bearer read-secret"})
                    self.assertEqual(status, 401)
                    self.assertIn("需要有效的访问凭证", body)
                    self.assertNotIn("dashboard.js", body)
                    self.assertEqual(headers["WWW-Authenticate"], "Bearer")
            for path in ("/?token=read-secret", "/web?token=read-secret", "/web/?token=read%2Dsecret"):
                status, headers, body = get(address, path)
                self.assertEqual(status, 200)
                self.assertIn("/web/dashboard.js", body)
                self.assertNotIn("read-secret", body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_page_is_disabled_without_a_configured_read_token(self):
        with web_server(None) as address:
            self.assertEqual(get(address, "/?token=anything")[0], 503)
            # Local collectors may still use their existing unauthenticated API.
            self.assertEqual(get(address, "/api/sessions")[0], 200)

    def test_read_api_keeps_bearer_auth_and_does_not_accept_query_token(self):
        with web_server() as address:
            self.assertEqual(get(address, "/api/sessions?token=read-secret")[0], 401)
            self.assertEqual(get(address, "/api/sessions", {"Authorization": "Bearer read-secret"})[0], 200)
            self.assertEqual(get(address, "/api")[0], 200)

    def test_only_named_assets_are_served_without_credentials(self):
        with web_server() as address:
            for path, content_type in (
                ("/web/dashboard.js", "text/javascript"), ("/web/client.mjs", "text/javascript"),
                ("/web/dashboard.css", "text/css"), ("/web/icon.svg", "image/svg+xml"),
            ):
                status, headers, body = get(address, path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            for path in ("/web/index.html", "/web/../api.py", "/web/%2e%2e/api.py", "/web/missing.js"):
                self.assertEqual(get(address, path)[0], 404)

    def test_url_encoded_special_characters_in_token(self):
        with web_server("a+b/c==&?") as address:
            self.assertEqual(get(address, "/?token=a%2Bb%2Fc%3D%3D%26%3F")[0], 200)


if __name__ == "__main__":
    unittest.main()
