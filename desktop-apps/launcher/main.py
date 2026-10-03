"""
Tuning Buddy desktop entry point: set up the environment, start the services, show the window,
and stop everything when the window closes.
"""
import ctypes
import logging
import os
import sys
import time

from . import runtime

logger = logging.getLogger("launcher")

ERROR_ALREADY_EXISTS = 183
_instance_mutex = None  # held for the life of the process


def _first_instance(wait_seconds: float = 5.0) -> bool:
    """Waits briefly so reopening right after closing doesn't collide with the copy still exiting."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\TuningBuddy.SingleInstance")
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            return True
        kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)


def main() -> int:
    runtime.redirect_output()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Imported after redirect_output(): these pull in pywebview/uvicorn, which touch stdout
    from . import window
    from .servers import Services

    if not _first_instance():
        window.message_box("Tuning Buddy is already running.")
        return 0

    services = None
    try:
        services = Services(runtime.setup_environment())
        if not window.run_window(services):
            window.run_in_browser(services)
    except Exception as e:
        logger.exception("Tuning Buddy could not start")
        window.message_box(f"Tuning Buddy could not start:\n\n{e}\n\nDetails: {runtime.log_path()}", error=True)
        return 1
    finally:
        if services is not None:
            services.stop()
    return 0


def run() -> None:
    # Run by the installer and uninstaller, without a window
    if "--setup-demo" in sys.argv:
        from . import demo_db
        os._exit(demo_db.run_setup_cli())
    if "--stop-demo" in sys.argv:
        from . import demo_db
        os._exit(demo_db.run_stop_cli())
    # os._exit: a long analysis may still hold a worker thread; closing the window must not hang
    os._exit(main())
