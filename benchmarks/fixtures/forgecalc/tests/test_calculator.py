from forgecalc.calculator import clamp, mean, percentage


def test_percentage_uses_percent_units() -> None:
    assert percentage(200, 15) == 30


def test_clamp() -> None:
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_mean() -> None:
    assert mean([2, 4, 6]) == 4
