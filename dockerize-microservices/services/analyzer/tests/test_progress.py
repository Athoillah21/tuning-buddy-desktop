"""Live progress for the analyze overlay: the registry and the reporter."""
import time

from app import progress

ID = 'a' * 32


def test_no_id_or_a_bad_id_gets_the_silent_reporter():
    assert isinstance(progress.reporter(None), progress.NoProgress)
    assert not isinstance(progress.reporter(None), progress.Progress)
    assert not isinstance(progress.reporter('../../etc'), progress.Progress)


def test_a_run_reports_phase_now_facts_recommendations_and_log():
    p = progress.reporter(ID)
    p.phase('baseline', 'Checking the query')
    p.now('Running your query with EXPLAIN ANALYZE, run 1 of 3')
    p.fact('baseline_ms', 201.8)
    p.rec(2, type='rewrite', status='waiting')
    p.rec(1, type='index', status='testing', attempt=1, max_attempts=5)
    p.rec(1, status='done', improvement=97.1, verdict='faster')
    state = progress.snapshot(ID)
    assert state['phase'] == 'baseline'
    assert state['now'].startswith('Running your query')
    assert state['facts']['baseline_ms'] == 201.8
    assert [r['index'] for r in state['recommendations']] == [1, 2]
    assert state['recommendations'][0] == {'index': 1, 'type': 'index', 'status': 'done', 'attempt': 1,
                                           'max_attempts': 5, 'improvement': 97.1, 'verdict': 'faster'}
    assert [line['text'] for line in state['log']] == ['Checking the query',
                                                      'Running your query with EXPLAIN ANALYZE, run 1 of 3']
    p.finish(True)
    finished = progress.snapshot(ID)
    assert finished['phase'] == 'done' and finished['ok'] is True and finished['finished_at']


def test_the_log_keeps_only_the_latest_lines_and_skips_repeats():
    p = progress.reporter('b' * 32)
    for i in range(30):
        p.now(f'step {i}')
        p.now(f'step {i}')
    log = progress.snapshot('b' * 32)['log']
    assert len(log) == progress.LOG_LINES
    assert log[-1]['text'] == 'step 29'
    assert len({line['text'] for line in log}) == len(log)


def test_finished_runs_are_forgotten_after_a_while(monkeypatch):
    p = progress.reporter('c' * 32)
    p.finish(False, 'boom')
    assert progress.snapshot('c' * 32)['error'] == 'boom'
    later = time.time() + progress.KEEP_SECONDS + 1
    monkeypatch.setattr(progress.time, 'time', lambda: later)
    progress.reporter('d' * 32)  # starting a run cleans up
    assert progress.snapshot('c' * 32) is None


def test_the_registry_is_bounded():
    for i in range(progress.MAX_RUNS + 10):
        progress.reporter(f"{i:032x}")
    assert len(progress._runs) <= progress.MAX_RUNS


def test_each_option_finishes_with_a_readable_line():
    from app.optimizer import _result_line
    assert _result_line(1, 'index', 'faster', 97.24, 'same') == "#1 index: 97% faster ✓"
    assert _result_line(2, 'rewrite', 'faster', 80.0, 'different') == "#2 rewrite: returns different rows ✕"
    assert _result_line(3, 'config', 'within_noise', 2.0, 'same') == "#3 config: no measurable change"
    assert _result_line(4, None, None, None, 'unchecked') == "#4 option: could not be tested"
