"""
The setup window's glass: clear, not frosted. The whole client area becomes DWM's "sheet of
glass" with no system backdrop, so the desktop shows through as it is, and the page's own tint
(app.css, like the Analyze overlay's) decides how see-through it is. Windows' acrylic backdrop was
tried first: it blurs and greys so heavily that over a normal desktop it reads as flat grey.

Windows 11 only (Windows 10, high contrast or TB_SETUP_NO_GLASS keep the page's own background).
"""
import ctypes
import os
import sys
from ctypes import wintypes

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
DWMSBT_NONE = 1  # no acrylic or mica: the desktop shows through unblurred
SPI_GETHIGHCONTRAST = 0x0042
HCF_HIGHCONTRASTON = 0x00000001
FIRST_WINDOWS_11_BUILD = 22000


class MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class HIGHCONTRAST(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("lpszDefaultScheme", wintypes.LPWSTR)]


def supported() -> bool:
    if os.name != "nt" or os.environ.get("TB_SETUP_NO_GLASS"):
        return False
    if sys.getwindowsversion().build < FIRST_WINDOWS_11_BUILD:
        return False
    contrast = HIGHCONTRAST(cbSize=ctypes.sizeof(HIGHCONTRAST))
    if ctypes.windll.user32.SystemParametersInfoW(SPI_GETHIGHCONTRAST, contrast.cbSize, ctypes.byref(contrast), 0):
        if contrast.dwFlags & HCF_HIGHCONTRASTON:
            return False
    return True


def _set(hwnd: int, attribute: int, value: int) -> bool:
    data = ctypes.c_int(value)
    result = ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(data),
                                                        ctypes.sizeof(data))
    return result == 0


def round_corners(hwnd: int) -> None:
    """Windows 11 rounds frameless windows only when asked; earlier versions ignore this."""
    try:
        _set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    except OSError:
        pass


def set_dark(hwnd: int, dark: bool) -> None:
    """The window's border and shadow follow light/dark."""
    try:
        _set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    except OSError:
        pass


def refresh(hwnd: int) -> None:
    """
    Put the glass back. Windows can drop it when it switches desktops (the admin prompt does) or
    the window loses focus to the install engine; the window then shows its black form instead.
    """
    try:
        margins = MARGINS(-1, -1, -1, -1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins))
        _set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
    except OSError:
        pass


WM_ACTIVATE = 0x0006
WM_SETTINGCHANGE = 0x001A
WM_NCACTIVATE = 0x0086
WM_THEMECHANGED = 0x031A
WM_DWMCOMPOSITIONCHANGED = 0x031E
_REFRESH_ON = {WM_ACTIVATE, WM_SETTINGCHANGE, WM_THEMECHANGED, WM_DWMCOMPOSITIONCHANGED}
_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
_hooks = []  # the callbacks must outlive the window


def keep(hwnd: int) -> None:
    """
    Keep the glass whatever happens to the window: it always draws as active (an inactive one can
    lose the glass), and the glass is put back after focus, theme or composition changes.
    """
    user32 = ctypes.windll.user32
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    previous = []

    def proc(h, message, wparam, lparam):
        if message == WM_NCACTIVATE:
            wparam = 1
        result = user32.CallWindowProcW(previous[0], h, message, wparam, lparam)
        if message in _REFRESH_ON:
            refresh(hwnd)
        return result

    callback = _WNDPROC(proc)
    _hooks.append(callback)
    previous.append(user32.SetWindowLongPtrW(wintypes.HWND(hwnd), -4, ctypes.cast(callback, ctypes.c_void_p)))


def apply(hwnd: int, dark: bool) -> bool:
    """
    Make the client area glass. The form behind the transparent WebView2 must be painted black:
    in an extended frame, that is what DWM treats as see-through.
    """
    try:
        margins = MARGINS(-1, -1, -1, -1)
        if ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(wintypes.HWND(hwnd), ctypes.byref(margins)) != 0:
            return False
        set_dark(hwnd, dark)
        round_corners(hwnd)
        _set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)  # explicit: no acrylic, no mica
        return True
    except OSError:
        return False
