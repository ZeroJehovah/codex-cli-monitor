"""Small, same-origin web frontend; session APIs retain their existing auth."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from urllib.parse import ParseResult, parse_qs


ASSETS = {
    "/web/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/web/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/web/client.mjs": ("client.mjs", "text/javascript; charset=utf-8"),
    "/web/icon.svg": ("icon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    ),
}


def serve_web(
    handler: BaseHTTPRequestHandler, parsed: ParseResult, api_token: str | None
) -> bool:
    """Serve only named packaged assets and a query-token-gated document."""
    status = 200
    if parsed.path in {"/", "/web", "/web/"}:
        content_type = "text/html; charset=utf-8"
        try:
            tokens = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32).get("token", [])
        except ValueError:
            tokens = []
        # Require an explicitly configured read token even on an otherwise
        # unauthenticated local collector. Never accept a write token/cookie.
        if not api_token:
            status = 503
        elif len(tokens) != 1 or not hmac.compare_digest(
            tokens[0].encode("utf-8"), api_token.encode("utf-8")
        ):
            status = 401
        name = "index.html" if status == 200 else "access.html"
    elif parsed.path in ASSETS:
        name, content_type = ASSETS[parsed.path]
    else:
        return False

    body = files("codex_cli_monitor").joinpath("web_assets", name).read_bytes()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in SECURITY_HEADERS.items():
        handler.send_header(key, value)
    if status == 401:
        handler.send_header("WWW-Authenticate", "Bearer")
    handler.end_headers()
    handler.wfile.write(body)
    return True
