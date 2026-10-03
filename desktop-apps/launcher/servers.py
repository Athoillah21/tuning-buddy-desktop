"""
Run the four services in-process: three FastAPI apps on uvicorn, Django on waitress
(gunicorn is POSIX-only). Service modules are imported here, after runtime.setup_environment().
"""
import logging
import threading
import time
import urllib.request

from . import demo_db
from .runtime import HOST, Ports

logger = logging.getLogger("launcher")

STARTUP_TIMEOUT = 90  # seconds; first start also runs the Django migrations


class Services:

    def __init__(self, ports: Ports):
        self.ports = ports
        self._uvicorn_servers = []
        self._waitress_server = None
        self._threads = []
        self._started = False
        self._demo_running = False

    @property
    def web_url(self) -> str:
        return f"http://{HOST}:{self.ports.web}/"

    def start(self) -> None:
        """Idempotent: the window and the browser fallback may both ask for it."""
        if self._started:
            return
        self._started = True

        self._migrate()
        self._start_demo_database()

        from ai_service.main import app as ai_app
        from analyzer_service.main import app as analyzer_app
        from report_service.main import app as report_app

        self._start_uvicorn(ai_app, self.ports.ai, "ai")
        self._start_uvicorn(analyzer_app, self.ports.analyzer, "analyzer")
        self._start_uvicorn(report_app, self.ports.report, "report")
        self._start_waitress()
        self._wait_until_ready()

    def stop(self, timeout: float = 2.0) -> None:
        """Ask every server to stop; whatever is still busy after `timeout` dies with the process."""
        if self._demo_running:
            try:
                demo_db.stop()
            except Exception:
                logger.exception("Could not stop the demo database")
        for server in self._uvicorn_servers:
            server.should_exit = True
        if self._waitress_server is not None:
            self._waitress_server.close()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _migrate(self) -> None:
        import django
        from django.core.management import call_command

        django.setup()
        # The command object, not its name: name lookup scans the filesystem, which a frozen
        # build does not have
        from django.core.management.commands import migrate
        call_command(migrate.Command(), interactive=False, verbosity=0)
        self._close_interrupted_analyses()

    @staticmethod
    def _close_interrupted_analyses() -> None:
        """
        An analysis runs inside its web request, so closing the window mid-analysis leaves its
        record at 'analyzing' forever. Nothing can be running yet at startup, so mark them failed.
        """
        from advisor.models import QueryHistory

        count = QueryHistory.objects.filter(analysis_status='analyzing').update(
            analysis_status='failed',
            error_message='Interrupted: Tuning Buddy was closed before this analysis finished.',
        )
        if count:
            logger.info("Marked %d interrupted analysis(es) as failed", count)

    def _start_demo_database(self) -> None:
        """Optional: the app works without it, so a failure is logged, not raised."""
        if not demo_db.is_installed():
            return
        try:
            port = demo_db.start()
            self._demo_running = True
            demo_db.ensure_connection(port)
        except Exception:
            logger.exception("The demo database is unavailable")

    def _start_uvicorn(self, app, port: int, name: str) -> None:
        import uvicorn

        # Explicit loop/http/ws: uvicorn's "auto" choices are imported dynamically, which a
        # frozen build cannot see
        config = uvicorn.Config(app, host=HOST, port=port, loop="asyncio", http="h11", ws="none",
                                lifespan="on", log_config=None, access_log=False)
        server = uvicorn.Server(config)
        self._uvicorn_servers.append(server)
        self._spawn(server.run, name)

    def _start_waitress(self) -> None:
        from waitress.server import create_server

        from tuning_buddy.wsgi import application

        # Long timeouts: an analysis request can run for up to ANALYZER_TIMEOUT (900s)
        self._waitress_server = create_server(application, host=HOST, port=self.ports.web, threads=8,
                                              channel_timeout=900, ident="TuningBuddy")
        self._spawn(self._waitress_server.run, "web")

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _wait_until_ready(self) -> None:
        pending = {
            "AI service": f"http://{HOST}:{self.ports.ai}/health",
            "analyzer service": f"http://{HOST}:{self.ports.analyzer}/health",
            "report service": f"http://{HOST}:{self.ports.report}/health",
            "web app": f"http://{HOST}:{self.ports.web}/healthz/",
        }
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while pending:
            for name, url in list(pending.items()):
                try:
                    with urllib.request.urlopen(url, timeout=2) as response:
                        if response.status == 200:
                            logger.info("%s is up at %s", name, url)
                            del pending[name]
                except OSError:
                    pass
            if not pending:
                return
            dead = [t.name for t in self._threads if not t.is_alive()]
            if dead:
                raise RuntimeError(f"Service stopped during startup: {', '.join(dead)} (see the log)")
            if time.monotonic() > deadline:
                raise RuntimeError(f"Timed out waiting for: {', '.join(pending)}")
            time.sleep(0.25)
