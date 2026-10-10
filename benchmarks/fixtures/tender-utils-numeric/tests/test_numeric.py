import pytest
from datetime import date
from tender_utils.numeric import clamp, percentage, mean, median, round_half_up, interpolate

def test_clamp():
    assert clamp(12, 0, 10) == 10
    assert clamp(-2, 0, 10) == 0
    assert clamp(5, 0, 10) == 5

def test_percentage():
    assert percentage(200, 15) == 30
    assert percentage(80, 25) == 20

def test_mean():
    assert mean([1, 2]) == 1.5
    assert mean([2, 4, 9]) == 5
    with pytest.raises(ValueError):
        mean([])

def test_median():
    assert median([9, 1, 5]) == 5
    assert median([8, 1, 6, 3]) == 4.5
    with pytest.raises(ValueError):
        median([])

def test_round_half_up():
    assert round_half_up(1.25, 1) == 1.3
    assert round_half_up(2.5) == 3
    assert round_half_up(-1.25, 1) == -1.3

def test_interpolate():
    assert interpolate(10, 20, 0) == 10
    assert interpolate(10, 20, 0.5) == 15
    assert interpolate(10, 20, 1) == 20
