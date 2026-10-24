"""Evaluator-owned target tests. They are never shown to the candidate."""

from stats.core import median


def test_median_even_length_averages_middle_values():
    assert median([4, 1, 3, 2]) == 2.5


def test_median_even_length_floats():
    assert median([1.0, 2.0]) == 1.5
