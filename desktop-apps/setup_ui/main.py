"""
TuningBuddySetup.exe and TuningBuddyUninstall.exe: one frameless window drawn in HTML (web/),
with the Inno Setup engine doing the actual work silently behind it.

The mode comes from the exe name (or --uninstall). Without WebView2 to draw the window, the engine's
own (skinned) wizard runs instead, so setup still works on a bare Windows 10.

For testing from source only (a built exe ignores the first two): TB_SETUP_ENGINE_DIR points at a
built engine; TB_SETUP_DEBUG_PORT opens WebView2's DevTools port; TB_SETUP_NO_WEBVIEW forces the
fallback.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from setup_ui import glass, installs

CREATE_NO_WINDOW = 0x08000000


def bundle_dir() -> Path:
    """Where the web/ files and the engine are: inside the onefile bundle, or the source tree."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def web_dir() -> Path:
    return bundle_dir() / "setup_ui" / "web"


def dev_switch(name: str) -> str:
    """Test switches only work from source: a released setup ignores them (they could swap the
    engine it runs as administrator, or open a remote-control port into its window)."""
    return "" if getattr(sys, "frozen", False) else os.environ.get(name, "")


def engine_dir() -> Path:
    override = dev_switch("TB_SETUP_ENGINE_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return bundle_dir() / "engine"
    return bundle_dir() / "build" / "engine"


def uninstall_mode() -> bool:
    return "--uninstall" in sys.argv or "uninstall" in Path(sys.executable).stem.lower()


def run_from_temp_copy() -> bool:
    """
    The uninstaller lives in the folder it removes, so it runs from a copy in %TEMP% (the stock
    uninstaller does the same). Returns True when this process started the copy and should exit.
    """
    if not getattr(sys, "frozen", False) or "--from-copy" in sys.argv:
        return False
    install = installs.find_install()
    here = Path(sys.executable).resolve()
    if not install or not install.get("location"):
        return False
    try:
        inside = here.parent == Path(install["location"]).resolve()
    except OSError:
        inside = False
    if not inside:
        return False  # the hand-out copy next to the README needs no copying
    copy = Path(tempfile.gettempdir()) / f"TuningBuddyUninstall-{os.getpid()}.exe"
    shutil.copy2(here, copy)
    subprocess.Popen([str(copy), "--from-copy", *[a for a in sys.argv[1:] if a != "--from-copy"]],
                     close_fds=True)
    return True


def delete_temp_copy_later() -> None:
    """The copy cannot delete itself while it runs: a hidden shell does it a moment after exit."""
    if "--from-copy" not in sys.argv:
        return
    me = sys.executable
    subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & del /f /q "{me}"',
                     creationflags=CREATE_NO_WINDOW, close_fds=True)


def fallback(uninstall: bool) -> int:
    """No WebView2: the classic (skinned) wizard of the engine or the stock uninstaller."""
    if uninstall:
        install = installs.find_install()
        program = (install or {}).get("uninstaller")
    else:
        program = str(engine_dir() / "TuningBuddyEngine.exe")
    if not program or not os.path.isfile(program):
        ctypes.windll.user32.MessageBoxW(None, "Tuning Buddy is not installed." if uninstall else
                                         "The installer is damaged. Download it again.", "Tuning Buddy", 0x40)
        return 1
    return subprocess.call([program])


def run() -> None:
    uninstall = uninstall_mode()
    if uninstall and run_from_temp_copy():
        return
    if not installs.webview2_available():
        sys.exit(fallback(uninstall))

    import webview
    from setup_ui.api import InstallApi, UninstallApi

    port = dev_switch("TB_SETUP_DEBUG_PORT")
    if port:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"

    api = UninstallApi() if uninstall else InstallApi(engine_dir())
    dark = installs.windows_uses_dark_theme()
    background = "#0b0e15" if dark else "#f4f6fb"
    api._glass = glass.supported()
    window = webview.create_window(
        "Uninstall Tuning Buddy" if uninstall else "Tuning Buddy Setup",
        url=(web_dir() / "index.html").as_uri(),
        js_api=api, width=760, height=500, resizable=False, frameless=True, easy_drag=False,
        shadow=True, background_color=background,
        transparent=api._glass,  # the WebView2 draws no background of its own; see glass.py
    )
    api._window = window  # private: pywebview exposes every public attribute to JavaScript

    def before_show(window):
        """On the UI thread, before the window first appears (so there is no flash)."""
        form = window.native
        api._hwnd = form.Handle.ToInt64()
        if not api._glass:
            glass.round_corners(api._hwnd)
            return
        from System.Drawing import Color, ColorTranslator
        form.BackColor = Color.Black
        if not glass.apply(api._hwnd, dark):
            api._glass = False  # info() tells the page, which then paints its own background
            form.BackColor = ColorTranslator.FromHtml(background)
            glass.round_corners(api._hwnd)
            return
        glass.keep(api._hwnd)  # through the admin prompt and the engine taking focus

    window.events.before_show += before_show

    # WebView2 keeps a profile folder; a throwaway one, so nothing is left behind
    profile = Path(tempfile.mkdtemp(prefix="tb-setup-webview-"))
    try:
        webview.start(gui="edgechromium", private_mode=True, storage_path=str(profile))
    except Exception:
        sys.exit(fallback(uninstall))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        if uninstall:
            delete_temp_copy_later()


if __name__ == "__main__":
    run()
