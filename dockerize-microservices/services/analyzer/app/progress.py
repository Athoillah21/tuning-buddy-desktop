"""
Live progress of an analysis, for the overlay the web app shows while it waits.

The web page picks a random id, sends it with the analysis request, and polls
GET /optimize/progress/{id} while the request runs. The optimizer reports what it is doing
through a Progress reporter; without an id it gets NoProgress, which ignores everything.
The analyzer runs one process, so the registry can live in memory.
"""
import re
import threading
import time
from typing import Any, Dict, Optional

PROGRESS_ID_RE = re.compile(r'[0-9a-f]{32}')
KEEP_SECONDS = 600     # finished runs stay readable this long
MAX_RUNS = 50
LOG_LINES = 12

_runs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


class NoProgress:
    """The reporter for runs nobody is watching."""

    def phase(self, name: str, now: Optional[str] = None) -> None:
        pass

    def now(self, text: str) -> None:
        pass

    def fact(self, key: str, value: Any) -> None:
        pass

    def rec(self, index: int, **fields) -> None:
        pass

    def log(self, text: str) -> None:
        pass

    def finish(self, ok: bool, error: Optional[str] = None) -> None:
        pass


class Progress(NoProgress):

    def __init__(self, progress_id: str):
        self.id = progress_id
        started = time.time()
        with _lock:
            _cleanup(started)
            _runs[progress_id] = {
                'phase': 'starting', 'now': 'Starting', 'facts': {}, 'recommendations': {},
                'log': [], 'started_at': started, 'finished_at': None, 'ok': None, 'error': None,
            }

    def _change(self, update):
        with _lock:
            run = _runs.get(self.id)
            if run is not None:
                update(run)

    def phase(self, name: str, now: Optional[str] = None) -> None:
        def update(run):
            run['phase'] = name
            if now:
                run['now'] = now
                _append_log(run, now)
        self._change(update)

    def now(self, text: str) -> None:
        def update(run):
            run['now'] = text
            _append_log(run, text)
        self._change(update)

    def fact(self, key: str, value: Any) -> None:
        self._change(lambda run: run['facts'].__setitem__(key, value))

    def rec(self, index: int, **fields) -> None:
        self._change(lambda run: run['recommendations'].setdefault(str(index), {'index': index}).update(fields))

    def log(self, text: str) -> None:
        self._change(lambda run: _append_log(run, text))

    def finish(self, ok: bool, error: Optional[str] = None) -> None:
        def update(run):
            run.update(phase='done' if ok else 'failed', ok=ok, error=error, finished_at=time.time(),
                       now='Done' if ok else (error or 'Failed'))
        self._change(update)


def _append_log(run: Dict[str, Any], text: str) -> None:
    if run['log'] and run['log'][-1]['text'] == text:
        return
    run['log'].append({'at': round(time.time() - run['started_at'], 1), 'text': text})
    del run['log'][:-LOG_LINES]


def _cleanup(now: float) -> None:
    """Drop finished runs after KEEP_SECONDS, and the oldest runs beyond MAX_RUNS."""
    for key in [k for k, r in _runs.items() if r['finished_at'] and now - r['finished_at'] > KEEP_SECONDS]:
        del _runs[key]
    if len(_runs) >= MAX_RUNS:
        for key in sorted(_runs, key=lambda k: _runs[k]['started_at'])[:len(_runs) - MAX_RUNS + 1]:
            del _runs[key]


def reporter(progress_id: Optional[str]) -> NoProgress:
    if progress_id and PROGRESS_ID_RE.fullmatch(progress_id):
        return Progress(progress_id)
    return NoProgress()


def snapshot(progress_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        run = _runs.get(progress_id)
        if run is None:
            return None
        result = {k: v for k, v in run.items()}
        result['facts'] = dict(run['facts'])
        result['recommendations'] = sorted((dict(r) for r in run['recommendations'].values()),
                                           key=lambda r: r['index'])
        result['log'] = list(run['log'])
    result['elapsed'] = round((result['finished_at'] or time.time()) - result['started_at'], 1)
    return result
