import pytest
from datetime import date
from tender_utils.collections import chunks, unique_preserving_order, flatten_once, deep_merge, percentile, partition

def test_chunks():
    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        chunks([1], 0)

def test_unique_preserving_order():
    assert unique_preserving_order(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]

def test_flatten_once():
    assert flatten_once([1, [2, 3], [[4]], 5]) == [1, 2, 3, [4], 5]

def test_deep_merge():
    left = {"db": {"host": "a", "port": 1}, "debug": True}
    right = {"db": {"port": 2}, "name": "x"}
    assert deep_merge(left, right) == {"db": {"host": "a", "port": 2}, "debug": True, "name": "x"}
    assert left["db"]["port"] == 1

def test_percentile():
    assert percentile([0, 10, 20, 30], 25) == 7.5
    assert percentile([3, 1, 2], 0) == 1
    assert percentile([3, 1, 2], 100) == 3
    with pytest.raises(ValueError):
        percentile([], 50)

def test_partition():
    assert partition([1, 2, 3, 4, 5], lambda x: x % 2 == 0) == ([2, 4], [1, 3, 5])
