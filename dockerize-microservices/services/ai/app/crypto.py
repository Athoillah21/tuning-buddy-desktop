"""
Fernet encryption for stored provider API keys.
"""
from cryptography.fernet import Fernet, InvalidToken

from . import config


class CryptoError(RuntimeError):
    """Raised when the encryption key is missing/invalid or a value cannot be decrypted."""
    pass


def _fernet() -> Fernet:
    if not config.ENCRYPTION_KEY:
        raise CryptoError("ENCRYPTION_KEY is not set")
    try:
        return Fernet(config.ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as e:
        raise CryptoError(f"ENCRYPTION_KEY is not a valid Fernet key: {e}") from e


def validate_key() -> None:
    """Fail fast at startup instead of silently storing plaintext keys."""
    _fernet()


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:
        raise CryptoError("Stored API key cannot be decrypted (was ENCRYPTION_KEY changed?)") from e


def mask(value: str) -> str:
    """Show just enough of a key to recognise it, never the whole thing."""
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}…{value[-4:]}"
