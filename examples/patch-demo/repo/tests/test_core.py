import pytest

from stats.core import mean, median


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_rejects_empty_input():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd_length():
    assert median([3, 1, 2]) == 2


def test_median_rejects_empty_input():
    with pytest.raises(ValueError):
        median([])


def test_median_does_not_mutate_input():
    values = [3, 1, 2]
    median(values)
    assert values == [3, 1, 2]
