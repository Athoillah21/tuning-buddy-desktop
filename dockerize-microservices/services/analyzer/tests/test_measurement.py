"""
A measured difference only counts when it is bigger than the noise.

These tests pin the honesty rules: sub-millisecond queries are never "improved",
and a gap smaller than the spread between repeats is not a gap.
"""
import pytest

from app.optimizer import classify_result, median_of, spread_of


def test_median_ignores_a_single_slow_outlier():
    assert median_of([10.0, 11.0, 90.0]) == 11.0


def test_median_of_one_value():
    assert median_of([4.2]) == 4.2


def test_spread_is_the_range_between_repeats():
    assert spread_of([10.0, 11.0, 14.0]) == pytest.approx(4.0)
    assert spread_of([7.0]) == 0.0


def test_fast_query_is_reported_as_already_fast_not_improved():
    """A 0.2ms primary key lookup used to report '+81% faster'."""
    result = classify_result([0.30, 0.19, 0.21], [0.06, 0.05, 0.07], noise_floor_ms=5.0)
    assert result['verdict'] == 'already_fast'


def test_difference_inside_the_spread_is_noise():
    # 40ms baseline, 38ms tested, but repeats vary by 6ms
    result = classify_result([44.0, 40.0, 38.0], [38.0, 37.0, 41.0], noise_floor_ms=5.0)
    assert result['verdict'] == 'within_noise'


def test_real_improvement_is_reported_as_faster():
    result = classify_result([161.0, 160.0, 162.0], [0.16, 0.15, 0.17], noise_floor_ms=5.0)
    assert result['verdict'] == 'faster'
    assert result['improvement_percentage'] == pytest.approx(99.9, abs=0.1)


def test_one_cold_run_does_not_hide_a_real_improvement():
    """JSONB expression index: 74ms -> 0.12ms was reported 'within_noise' (spread 98.8ms)."""
    result = classify_result([73.316, 74.087, 172.127], [0.101, 0.116, 0.146], noise_floor_ms=5.0)
    assert result['verdict'] == 'faster'


def test_non_overlapping_repeats_are_a_real_difference():
    # pgvector HNSW: 8.8-30.8ms before, 2.8-3.0ms after
    result = classify_result([8.778, 8.951, 30.849], [2.802, 2.866, 2.97], noise_floor_ms=5.0)
    assert result['verdict'] == 'faster'


def test_regression_is_reported_as_slower():
    result = classify_result([40.0, 41.0, 39.0], [80.0, 82.0, 81.0], noise_floor_ms=5.0)
    assert result['verdict'] == 'slower'
    assert result['improvement_percentage'] < 0


def test_medians_and_spread_are_returned_for_the_report():
    result = classify_result([100.0, 110.0, 120.0], [10.0, 12.0, 11.0], noise_floor_ms=5.0)
    assert result['median_original_ms'] == 110.0
    assert result['median_tested_ms'] == 11.0
    assert result['measurement_spread_ms'] == pytest.approx(20.0)


def test_missing_measurements_do_not_crash():
    result = classify_result([], [], noise_floor_ms=5.0)
    assert result['verdict'] == 'unknown'
    assert result['improvement_percentage'] == 0
