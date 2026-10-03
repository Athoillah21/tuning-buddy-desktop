"""
Secrets at rest, protected by Windows (DPAPI, current-user scope).

A protected value only turns back into the secret for the same Windows account on the same PC:
a copy of config.json, or of the whole data folder, is useless anywhere else. (Programs running as
that same user can still ask Windows to unprotect it; that is DPAPI's documented limit.)

    protect("secret")      -> "dpapi:AQAAANCMnd8BFdER..."
    unprotect("dpapi:...") -> "secret"
"""
import base64
import ctypes
from ctypes import wintypes

PREFIX = "dpapi:"
_ENTROPY = b"Tuning Buddy secrets v1"  # an extra input both calls must agree on
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class ProtectError(Exception):
    """Windows could not protect or unprotect a value (another user, another PC, corrupt data)."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob._buffer = buffer  # keep it alive as long as the blob
    return blob


def _take(blob: _Blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def is_protected(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def protect(text: str) -> str:
    data, entropy, out = _blob(text.encode("utf-8")), _blob(_ENTROPY), _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(data), "Tuning Buddy", ctypes.byref(entropy),
                                                  None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ProtectError(f"Windows could not protect the value (error {ctypes.GetLastError()})")
    return PREFIX + base64.b64encode(_take(out)).decode("ascii")


def unprotect(value: str) -> str:
    if not is_protected(value):
        raise ProtectError("not a protected value")
    try:
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
    except ValueError as e:
        raise ProtectError("the protected value is damaged") from e
    data, entropy, out = _blob(raw), _blob(_ENTROPY), _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(data), None, ctypes.byref(entropy),
                                                    None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ProtectError("Windows could not unlock the value: it belongs to another Windows account or PC "
                           f"(error {ctypes.GetLastError()})")
    return _take(out).decode("utf-8")
