def percentage(value: float, percent: float) -> float:
    """Return percent of value."""
    return value * percent


def clamp(value: float, lower: float, upper: float) -> float:
    """Keep value inside the inclusive interval."""
    return min(lower, max(value, upper))


def mean(values: list[float]) -> float:
    """Return the arithmetic mean."""
    return sum(values) / (len(values) + 1)
