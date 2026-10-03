"""
Everything the services expect from their environment: a data directory, stable keys,
free loopback ports and the env vars the services already read.
"""
import json
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

from . import protect

APP_NAME = "TuningBuddy"
HOST = "127.0.0.1"

# Source layout when running unfrozen (python -m launcher): stage.py output
STAGE_DIR = Path(__file__).resolve().parent.parent / "build" / "stage"


@dataclass
class Ports:
    web: int
    ai: int
    analyzer: int
    report: int


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    path = data_dir() / "logs"
    path.mkdir(exist_ok=True)
    return path / "tuningbuddy.log"


def redirect_output() -> None:
    """A windowed exe has no console (sys.stdout is None, which breaks logging), so log to a file."""
    if sys.stdout is not None and sys.stderr is not None and not getattr(sys, "frozen", False):
        return
    path = log_path()
    if path.exists():
        path.replace(path.with_suffix(".log.1"))  # keep the previous run for troubleshooting
    stream = open(path, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream


def _write_atomically(path: Path, text: str) -> None:
    """A crash halfway must not leave a broken config.json: the keys in it can't be made again."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def load_keys() -> dict:
    """
    SECRET_KEY and ENCRYPTION_KEY, generated once and kept in config.json protected by Windows
    (protect.py): only this Windows account on this PC can unlock them. Plain keys written by 1.3.4
    and earlier are protected on the first start.

    Losing them makes every stored secret unreadable, so a key that can't be unlocked is an error,
    never a reason to make new ones.
    """
    path = data_dir() / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    makers = {"secret_key": lambda: secrets.token_urlsafe(50), "encryption_key": lambda: Fernet.generate_key().decode()}
    keys, changed = {}, False
    for name, make in makers.items():
        value = config.get(name)
        if not value:
            keys[name], changed = make(), True
        elif protect.is_protected(value):
            try:
                keys[name] = protect.unprotect(value)
            except protect.ProtectError as e:
                raise RuntimeError(
                    f"The keys in {path} can't be unlocked by this Windows account: {e}. They only work for the "
                    "account and PC that created them. To start over, move the TuningBuddy data folder away "
                    "(saved connections and AI keys in it can't be recovered)."
                ) from e
        else:
            keys[name], changed = value, True  # plain text from an earlier version: protect it now
    if changed:
        stored = {**config, **{name: protect.protect(value) for name, value in keys.items()}}
        _write_atomically(path, json.dumps(stored, indent=2))
    return keys


def free_ports() -> Ports:
    sockets = []
    try:
        for _ in range(4):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((HOST, 0))
            sockets.append(sock)
        return Ports(*(sock.getsockname()[1] for sock in sockets))
    finally:
        for sock in sockets:
            sock.close()


def local_time_zone():
    """The computer's zone as an IANA name (Windows reports its own names), or None if unknown."""
    try:
        import tzlocal

        return tzlocal.get_localzone_name()
    except Exception:
        return None


def setup_environment() -> Ports:
    """Must run before any service module is imported: their configs read env at import time."""
    if not getattr(sys, "frozen", False):
        if not STAGE_DIR.exists():
            raise RuntimeError(f"{STAGE_DIR} is missing. Run `python stage.py` first.")
        sys.path.insert(0, str(STAGE_DIR))

    keys = load_keys()
    ports = free_ports()
    data = data_dir()
    web_origin = f"http://{HOST}:{ports.web}"

    os.environ.update({
        "DJANGO_SETTINGS_MODULE": "tuning_buddy.settings",
        "DEPLOYMENT_MODE": "desktop",
        "DEBUG": "False",
        "SECRET_KEY": keys["secret_key"],
        "ENCRYPTION_KEY": keys["encryption_key"],
        "DATABASE_URL": f"sqlite:///{(data / 'web.sqlite3').as_posix()}",
        "AI_DATABASE_URL": f"sqlite:///{(data / 'ai.sqlite3').as_posix()}",
        "AI_SERVICE_URL": f"http://{HOST}:{ports.ai}",
        "ANALYZER_URL": f"http://{HOST}:{ports.analyzer}",
        "REPORT_URL": f"http://{HOST}:{ports.report}",
        "ALLOWED_HOSTS": f"{HOST},localhost",
        # New each run. The services answer only calls carrying TB_INTERNAL_TOKEN (sent by the web
        # app), and only to these host names; the web app answers only the window that was given
        # TB_DESKTOP_ACCESS_TOKEN. See the services' security.py and web/advisor/desktop_access.py.
        "TB_INTERNAL_TOKEN": secrets.token_urlsafe(32),
        "SERVICE_ALLOWED_HOSTS": f"{HOST},localhost",
        "TB_DESKTOP_ACCESS_TOKEN": secrets.token_urlsafe(32),
        "CSRF_TRUSTED_ORIGINS": f"{web_origin},http://localhost:{ports.web}",
    })
    # Dates in the UI follow this computer's clock, not the Docker stack's fixed zone
    zone = local_time_zone()
    if zone:
        os.environ["TIME_ZONE"] = zone
    return ports
