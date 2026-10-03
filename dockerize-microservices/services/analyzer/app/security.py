"""
Who may call this service. Both checks are off unless their env var is set: the Docker stack leaves
them unset; the desktop launcher sets both (its services all listen on 127.0.0.1, which every
program and every Windows account on the PC can reach).

TB_INTERNAL_TOKEN      Every request except GET /health must carry it in X-Tuning-Buddy-Token.
                       The launcher makes a new one each run; only the web app is given it.
SERVICE_ALLOWED_HOSTS  Comma-separated Host names this service answers to. Anything else gets 400:
                       a website using DNS rebinding to reach 127.0.0.1 sends its own host name.

The same file is in each service (ai, analyzer, report), like their config.py.
"""
import hmac
import json
import os

TOKEN_HEADER = b"x-tuning-buddy-token"
OPEN_PATHS = {"/health"}


def _host_name(host: str) -> str:
    """'127.0.0.1:8001' -> '127.0.0.1', '[::1]:8001' -> '[::1]'."""
    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


async def _refuse(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class ServiceGuard:
    """ASGI middleware: app.add_middleware(ServiceGuard)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])

        allowed = {h.strip().lower() for h in os.environ.get("SERVICE_ALLOWED_HOSTS", "").split(",") if h.strip()}
        if allowed and _host_name(headers.get(b"host", b"").decode("latin-1")) not in allowed:
            await _refuse(send, 400, "Invalid host")
            return

        token = os.environ.get("TB_INTERNAL_TOKEN", "")
        if token and scope.get("path") not in OPEN_PATHS:
            if not hmac.compare_digest(headers.get(TOKEN_HEADER, b""), token.encode()):
                await _refuse(send, 401, "This service only answers Tuning Buddy itself")
                return

        await self.app(scope, receive, send)
