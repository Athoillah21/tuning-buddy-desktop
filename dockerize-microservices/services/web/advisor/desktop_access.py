"""
The desktop app opens only in its own window.

The launcher makes a new DESKTOP_ACCESS_TOKEN each run and opens the window at
/desktop/open/?key=<token>. That sets a session cookie (signed, HttpOnly, SameSite=Strict) and
redirects to the dashboard, so the key leaves the address bar and history. Every other page then
needs the cookie: another program, another Windows account or a web page that finds the port gets
a 403. The cookie dies with the window, and the next run has a new key anyway.

With DESKTOP_ACCESS_TOKEN unset (the Docker stack) nothing changes.
"""
import hmac

from django.conf import settings
from django.core import signing
from django.http import HttpResponse, HttpResponseRedirect

COOKIE = "tb_window"
OPEN_PATH = "/desktop/open/"
SALT = "tuning-buddy.desktop-access"
ALWAYS_OPEN = ("/healthz/", "/static/")

REFUSED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Tuning Buddy</title>
<style>body{font:15px/1.5 "Segoe UI",system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;
height:100vh;margin:0}div{max-width:420px;text-align:center}h1{font-size:20px;font-weight:600}p{color:#aaa}</style></head>
<body><div><h1>Open Tuning Buddy from its own window</h1>
<p>For your safety, Tuning Buddy only answers its own window. Start it from the Start menu or the
desktop shortcut.</p></div></body></html>"""


def _token() -> str:
    return getattr(settings, "DESKTOP_ACCESS_TOKEN", "") or ""


def _cookie_value(token: str) -> str:
    # Signed with SECRET_KEY and bound to this run's token: a cookie from an earlier run is useless
    return signing.dumps({"w": hmac.new(token.encode(), b"window", "sha256").hexdigest()}, salt=SALT)


def _cookie_ok(value: str, token: str) -> bool:
    try:
        data = signing.loads(value or "", salt=SALT)
    except signing.BadSignature:
        return False
    expected = hmac.new(token.encode(), b"window", "sha256").hexdigest()
    return isinstance(data, dict) and hmac.compare_digest(str(data.get("w", "")), expected)


class DesktopAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _token()
        if not token:
            return self.get_response(request)

        if request.path == OPEN_PATH:
            if not hmac.compare_digest(request.GET.get("key", ""), token):
                return HttpResponse(REFUSED_PAGE, status=403)
            response = HttpResponseRedirect("/")
            response.set_cookie(COOKIE, _cookie_value(token), httponly=True, samesite="Strict", path="/")
            return response

        if request.path.startswith(ALWAYS_OPEN) or _cookie_ok(request.COOKIES.get(COOKIE, ""), token):
            return self.get_response(request)
        return HttpResponse(REFUSED_PAGE, status=403)
