"""
The optional demo database: a private PostgreSQL 16 (with PostGIS and pgvector) that ships in
{app}\\pgsql and keeps its data in %LOCALAPPDATA%\\TuningBuddy\\demo-db. It is seeded once,
by the installer (`TuningBuddy.exe --setup-demo`), with the benchmark data the Docker stack's
sampledb uses, and the app starts it on 127.0.0.1 while its window is open.
"""
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import protect, runtime

logger = logging.getLogger("launcher")

DATABASE = "shop"
USER = "demo"
PUBLIC_PASSWORD = "demo"  # what installs before 1.3.6 used; replaced on their next start
SUPERUSER = "postgres"
CONNECTION_NAME = "Demo database"
PREFERRED_PORT = 55432
# Seeds need PostGIS and pgvector, which are not trusted extensions, so the superuser creates them
EXTENSIONS = ["postgis", "vector", "pg_trgm"]

CREATE_NO_WINDOW = 0x08000000

# What each seed loads, as the installer window shows it
SEED_LABELS = {
    "01_shop": "Loading the shop tables · customers, orders, products",
    "02_large_orders": "Loading large_orders · 1M rows",
    "03_spatial": "Loading places · 200k PostGIS points",
    "04_vector": "Loading documents · 20k pgvector embeddings",
    "05_events": "Loading events · 300k JSONB rows",
    "06_partitioned": "Loading measurements · 600k rows in 12 partitions",
}

# A demo server on a laptop: keep the write-ahead log small. PostgreSQL's default lets it grow to
# 1 GB, and every analysis writes full table copies into it. With wal_level=minimal, copies made
# by CREATE TABLE ... AS skip the WAL altogether. Applied to existing installs on the next start.
DISK_SETTINGS_MARKER = "# Tuning Buddy: small WAL"
DISK_SETTINGS = (
    f"\n{DISK_SETTINGS_MARKER} (a local demo needs no replication or point-in-time recovery)\n"
    "wal_level = minimal\n"
    "max_wal_senders = 0\n"
    "max_wal_size = 128MB\n"
    "min_wal_size = 32MB\n"
)
STOP_TIMEOUT = 30  # seconds


def pgsql_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "pgsql"
    return Path(__file__).resolve().parent.parent / "build" / "pgsql"


def seeds_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "demo_seeds"
    return Path(__file__).resolve().parent.parent.parent / "dockerize-microservices" / "infra" / "sampledb" / "init"


def root_dir() -> Path:
    return runtime.data_dir() / "demo-db"


def _data() -> Path:
    return root_dir() / "data"


def _state_path() -> Path:
    return root_dir() / "demo.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    root_dir().mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Passwords: random per install, kept in demo.json protected by Windows (protect.py)
# ---------------------------------------------------------------------------

def _secret(state: dict, name: str) -> str:
    """A password from demo.json: protected (1.3.6 on) or, from older installs, plain."""
    value = state.get(name) or ""
    return protect.unprotect(value) if protect.is_protected(value) else value


def demo_password() -> str:
    """The demo user's password (the bundled connection uses it; shown on its panel for other tools)."""
    return _secret(load_state(), "demo_password") or PUBLIC_PASSWORD


def _secure_credentials(port: int) -> None:
    """
    Installs from before 1.3.6: protect the plain superuser password, and give the demo user a
    random password instead of the public "demo". The new password is saved only once the server
    has accepted it, so a failure leaves everything working as before.
    """
    state = load_state()
    changed = False
    superuser = _secret(state, "superuser_password")
    if superuser and not protect.is_protected(state.get("superuser_password")):
        state["superuser_password"] = protect.protect(superuser)
        changed = True
    if superuser and not state.get("demo_password"):
        new_password = secrets.token_urlsafe(24)
        try:
            conn = _connect(port, SUPERUSER, superuser, "postgres")
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"ALTER ROLE {USER} PASSWORD %s", (new_password,))
            conn.close()
            state["demo_password"] = protect.protect(new_password)
            changed = True
            logger.info("The demo user now has its own random password")
        except Exception:
            logger.warning("Could not give the demo user a random password; it keeps the old one", exc_info=True)
    if changed:
        save_state(state)


def is_installed() -> bool:
    """Seeded here, and the server binaries are still installed."""
    return bool(load_state().get("seeded")) and (pgsql_dir() / "bin" / "pg_ctl.exe").exists()


def _run(program: str, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    """A PostgreSQL program without a console window. Output goes to a file, not a pipe: a
    server started by pg_ctl inherits the pipe and would keep it open after pg_ctl exits."""
    log = root_dir() / "pg_tools.log"
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"\n$ {program} {' '.join(args)}\n")
        out.flush()
        return subprocess.run([str(pgsql_dir() / "bin" / program), *args], stdin=subprocess.DEVNULL,
                              stdout=out, stderr=subprocess.STDOUT, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)


def _tools_log_tail(lines: int = 15) -> str:
    try:
        return "\n".join((root_dir() / "pg_tools.log").read_text(encoding="utf-8").splitlines()[-lines:])
    except OSError:
        return ""


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((runtime.HOST, port))
            return True
        except OSError:
            return False


def _pick_port(previous: int = 0) -> int:
    for port in (previous, PREFERRED_PORT):
        if port and _port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((runtime.HOST, 0))
        return sock.getsockname()[1]


def _running_port():
    """The port of a server already running on our data directory (e.g. left by a crash), or None."""
    pid_file = _data() / "postmaster.pid"
    if not pid_file.exists() or _run("pg_ctl.exe", "status", "-D", str(_data()), timeout=30).returncode != 0:
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").splitlines()[3])
    except (OSError, ValueError, IndexError):
        return None


def _ensure_disk_settings() -> bool:
    """Add the small-WAL settings to postgresql.conf once. True if they were just added."""
    conf = _data() / "postgresql.conf"
    text = conf.read_text(encoding="utf-8")
    if DISK_SETTINGS_MARKER in text:
        return False
    with open(conf, "a", encoding="utf-8") as handle:
        handle.write(DISK_SETTINGS)
    return True


def _release_wal(port: int) -> None:
    """
    Shrink pg_wal to the new limit now instead of over the next weeks. Segments the old 1 GB limit
    pre-allocated are only removed once used, so step through them (a tiny WAL record, then a
    switch to the next file) and checkpoint twice: with the small limit they are deleted, not kept.
    """
    password = _secret(load_state(), "superuser_password")
    if not password:
        return
    try:
        conn = _connect(port, SUPERUSER, password, "postgres")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_ls_waldir()")
            for _ in range(cur.fetchone()[0]):
                cur.execute("SELECT pg_logical_emit_message(false, 'tuning-buddy', 'release')")
                cur.execute("SELECT pg_switch_wal()")
            cur.execute("CHECKPOINT")
            cur.execute("CHECKPOINT")
            cur.execute("SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir()")
            logger.info("Demo database WAL is now %s", cur.fetchone()[0])
        conn.close()
    except Exception:
        logger.warning("Could not run a checkpoint on the demo database", exc_info=True)


def start() -> int:
    """Start the demo server (or adopt one already running) and return its port."""
    added = _ensure_disk_settings()
    port = _running_port()
    if port:
        # Settings like wal_level apply at the server's next start; the WAL limit already helps
        logger.info("Demo database is already running on port %d", port)
        _secure_credentials(port)
        return port
    state = load_state()
    port = _pick_port(state.get("port", 0))
    result = _run("pg_ctl.exe", "start", "-D", str(_data()), "-w", "-t", "60",
                  "-l", str(root_dir() / "server.log"), "-o", f"-p {port}", timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"The demo database did not start (pg_ctl exit {result.returncode}). "
                           f"See {root_dir() / 'server.log'}")
    if state.get("port") != port:
        state["port"] = port
        save_state(state)
    logger.info("Demo database is up on port %d", port)
    _secure_credentials(port)
    if added:
        logger.info("Applied the small-WAL settings to the demo database; releasing old WAL")
        _release_wal(port)
    return port


def stop() -> None:
    if not (_data() / "postmaster.pid").exists():
        return
    result = _run("pg_ctl.exe", "stop", "-D", str(_data()), "-m", "fast", "-w", "-t", str(STOP_TIMEOUT),
                  timeout=STOP_TIMEOUT + 10)
    if result.returncode != 0:
        logger.warning("pg_ctl stop exited with %d", result.returncode)


def _connect(port: int, user: str, password: str, database: str):
    import psycopg2

    return psycopg2.connect(host=runtime.HOST, port=port, user=user, password=password,
                            dbname=database, connect_timeout=10)


def setup(progress=print) -> None:
    """Create and seed the demo database. Safe to run again: a finished setup is kept."""
    if load_state().get("seeded") and _data().exists():
        progress("The demo database is already set up.")
        return
    if not (pgsql_dir() / "bin" / "initdb.exe").exists():
        raise RuntimeError(f"PostgreSQL is not installed at {pgsql_dir()}")

    # A half-finished earlier attempt: start over
    if _data().exists():
        stop()
        shutil.rmtree(_data())
    root_dir().mkdir(parents=True, exist_ok=True)

    superuser_password = secrets.token_urlsafe(24)
    user_password = secrets.token_urlsafe(24)
    save_state({"superuser_password": protect.protect(superuser_password),
                "demo_password": protect.protect(user_password), "seeded": False})

    progress("Creating the demo database server")
    pwfile = root_dir() / "pwfile.tmp"
    pwfile.write_text(superuser_password, encoding="utf-8")
    try:
        result = _run("initdb.exe", "-D", str(_data()), "-U", SUPERUSER, f"--pwfile={pwfile}",
                      "-A", "scram-sha-256", "-E", "UTF8", "--locale=C")
    finally:
        pwfile.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"initdb failed (exit {result.returncode}):\n{_tools_log_tail()}")

    with open(_data() / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write("\n# Tuning Buddy demo database: local only, modest resources\n"
                   "listen_addresses = '127.0.0.1'\n"
                   "max_connections = 30\n"
                   "shared_buffers = 128MB\n")

    port = start()
    try:
        progress("Creating the demo user and extensions")
        admin = _connect(port, SUPERUSER, superuser_password, "postgres")
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"CREATE ROLE {USER} LOGIN PASSWORD %s", (user_password,))
            cur.execute(f"CREATE DATABASE {DATABASE} OWNER {USER}")
        admin.close()
        admin = _connect(port, SUPERUSER, superuser_password, DATABASE)
        admin.autocommit = True
        with admin.cursor() as cur:
            for extension in EXTENSIONS:
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
        admin.close()

        # As the demo user, so it owns the tables (and can index them when testing)
        demo = _connect(port, USER, user_password, DATABASE)
        demo.autocommit = True
        for seed in sorted(seeds_dir().glob("*.sql")):
            progress(SEED_LABELS.get(seed.stem, f"Loading {seed.stem}"))
            started = time.monotonic()
            with demo.cursor() as cur:
                cur.execute(seed.read_text(encoding="utf-8"))
            logger.info("Seeded %s in %.1fs", seed.name, time.monotonic() - started)
        demo.close()
    finally:
        stop()

    state = load_state()
    state.update({"seeded": True, "seeded_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    save_state(state)
    progress("The demo database is ready.")


def ensure_connection(port: int) -> None:
    """
    Add the demo database to Connections once, and keep its port current. The web app learns
    which connection it is from DEMO_CONNECTION_ID: it offers the test cases for it and never
    asks for its password again (the app keeps it). Must run after django.setup().
    """
    from advisor.models import Connection

    state = load_state()
    connection = Connection.objects.filter(pk=state["connection_id"]).first() if state.get("connection_id") else None
    if connection is None:
        if state.get("connection_id"):
            return  # the user deleted it; respect that
        connection = Connection(name=CONNECTION_NAME, host=runtime.HOST, database=DATABASE, username=USER)
        logger.info("Adding the demo database to Connections")
    connection.port = port
    connection.ssl_mode = "disable"
    connection.password = demo_password()  # plain text: save() encrypts it and restarts the expiry clock
    connection.save()
    if state.get("connection_id") != connection.pk:
        state["connection_id"] = connection.pk
        save_state(state)
    os.environ["DEMO_CONNECTION_ID"] = str(connection.pk)


class SetupCancelled(Exception):
    """The installer window's Cancel, noticed between two steps."""


EXIT_CANCELLED = 3  # the install engine reads this as "cancelled", not "failed"


def run_setup_cli() -> int:
    """
    `TuningBuddy.exe --setup-demo [--progress-file PATH]`, run by the install engine. Exit code 0 on
    success. With --progress-file, each step is also appended there as a line, which the custom
    installer window shows while it waits; and PATH.cancel appearing means its Cancel was clicked:
    the setup stops after the current step and removes the half-loaded demo database (exit 3).
    """
    root_dir().mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(root_dir() / "setup.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    progress_file = None
    if "--progress-file" in sys.argv:
        index = sys.argv.index("--progress-file")
        if index + 1 < len(sys.argv):
            progress_file = Path(sys.argv[index + 1])

    def progress(text: str) -> None:
        if progress_file is not None and Path(f"{progress_file}.cancel").exists():
            raise SetupCancelled()
        logger.info(text)
        if progress_file is not None:
            try:
                with open(progress_file, "a", encoding="utf-8") as out:
                    out.write(text + "\n")
            except OSError:
                pass  # the window is a nicety; the setup itself must not fail over it

    try:
        setup(progress=progress)
        return 0
    except SetupCancelled:
        logger.info("Cancelled from the installer window")
        if load_state().get("seeded"):
            return EXIT_CANCELLED  # a finished demo database from before stays
        stop()
        logging.getLogger().removeHandler(handler)
        handler.close()  # it holds setup.log, inside the folder that goes
        shutil.rmtree(root_dir(), ignore_errors=True)
        return EXIT_CANCELLED
    except Exception:
        logger.exception("Demo database setup failed")
        return 1


def run_stop_cli() -> int:
    """`TuningBuddy.exe --stop-demo`: stop a demo server left running, so its files can be replaced
    or removed. Only acts when a demo database exists here."""
    if not _data().exists() or not (pgsql_dir() / "bin" / "pg_ctl.exe").exists():
        return 0
    try:
        stop()
        return 0
    except Exception:
        return 1
