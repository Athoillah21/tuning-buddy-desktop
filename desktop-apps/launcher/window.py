"""
The app window: WebView2 via pywebview, or the default browser when that is unavailable.
"""
import ctypes
import html
import logging
import os
import webbrowser
from urllib.parse import quote

from . import runtime
from .servers import Services

logger = logging.getLogger("launcher")

TITLE = "Tuning Buddy"

MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{ --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --accent: #0066ff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #0f1115; --fg: #e5e7eb; --muted: #9ca3af; --accent: #3b82f6; }} }}
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: flex; align-items: center; justify-content: center; background: var(--bg); color: var(--fg);
         font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width: 520px; padding: 24px; text-align: center; }}
  h1 {{ font-size: 20px; font-weight: 600; margin: 16px 0 6px; }}
  p {{ color: var(--muted); margin: 0; }}
  code {{ font-size: 13px; word-break: break-all; }}
  .spinner {{ width: 28px; height: 28px; margin: 0 auto; border: 3px solid var(--muted); border-top-color: var(--accent);
             border-radius: 50%; opacity: .8; animation: spin .8s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style></head><body><main>{body}</main></body></html>"""

LOADING_HTML = _PAGE.format(body='<div class="spinner"></div><h1>Starting Tuning Buddy</h1><p>This takes a few seconds.</p>')


def error_html(error: Exception) -> str:
    return _PAGE.format(body=(
        f"<h1>Tuning Buddy could not start</h1><p>{html.escape(str(error))}</p>"
        f"<p style='margin-top:12px'>Details are in <code>{html.escape(str(runtime.log_path()))}</code></p>"
    ))


def message_box(text: str, error: bool = False) -> None:
    flags = (MB_ICONERROR if error else MB_ICONINFORMATION) | MB_SETFOREGROUND
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)


def window_url(services: Services) -> str:
    """The app answers only a window opened with this run's key (web/advisor/desktop_access.py)."""
    key = os.environ.get("TB_DESKTOP_ACCESS_TOKEN", "")
    return f"{services.web_url}desktop/open/?key={quote(key)}" if key else services.web_url


def run_window(services: Services) -> bool:
    """Blocks until the window closes. False if no native window could be created."""
    try:
        import webview
    except Exception:
        logger.exception("pywebview is unavailable")
        return False

    webview.settings["ALLOW_DOWNLOADS"] = True  # the PDF report
    window = webview.create_window(TITLE, html=LOADING_HTML, width=1280, height=840, min_size=(960, 640))

    def boot():
        try:
            services.start()
            window.load_url(window_url(services))
        except Exception as e:
            logger.exception("Startup failed")
            window.load_html(error_html(e))

    try:
        # storage_path: WebView2 would otherwise create its profile next to the exe,
        # which is read-only under Program Files
        webview.start(boot, gui="edgechromium", private_mode=False,
                      storage_path=str(runtime.data_dir() / "webview"))
    except Exception:
        logger.exception("The native window failed; falling back to the browser")
        return False
    return True


def run_in_browser(services: Services) -> None:
    services.start()
    webbrowser.open(window_url(services))
    message_box(f"Tuning Buddy is running in your web browser at {services.web_url}\n\n"
                "Click OK to stop Tuning Buddy.")
