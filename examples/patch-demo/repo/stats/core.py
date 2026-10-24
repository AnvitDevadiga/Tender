"""Small statistics helpers used by the Tender patch-verification demo."""


def mean(values):
    """Return the arithmetic mean of a non-empty sequence."""
    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / len(values)


def median(values):
    """Return the median of a non-empty sequence."""
    if not values:
        raise ValueError("median() requires at least one value")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
