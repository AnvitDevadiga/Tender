import pytest
from datetime import date
from tender_utils.dates import date_range, add_months, business_days, is_leap_year, next_weekday, format_duration

def test_date_range():
    assert [d.isoformat() for d in date_range(date(2024, 2, 28), date(2024, 3, 1))] == ["2024-02-28", "2024-02-29", "2024-03-01"]
    assert date_range(date(2024, 2, 2), date(2024, 2, 1)) == []

def test_add_months():
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)

def test_business_days():
    assert business_days(date(2024, 5, 3), date(2024, 5, 6)) == 1
    assert business_days(date(2024, 5, 6), date(2024, 5, 10)) == 4
    assert business_days(date(2024, 5, 8), date(2024, 5, 7)) == 0

def test_is_leap_year():
    assert is_leap_year(2000) is True
    assert is_leap_year(1900) is False
    assert is_leap_year(2024) is True
    assert is_leap_year(2023) is False

def test_next_weekday():
    assert next_weekday(date(2024, 5, 6), 0) == date(2024, 5, 13)
    assert next_weekday(date(2024, 5, 6), 2) == date(2024, 5, 8)

def test_format_duration():
    assert format_duration(3661) == "1h 1m 1s"
    assert format_duration(60) == "1m"
    assert format_duration(0) == "0s"
    with pytest.raises(ValueError):
        format_duration(-1)
