from datetime import date, timedelta

def date_range(start, end):
    result = []
    current = start
    while current < end:
        result.append(current)
        current += timedelta(days=1)
    return result

def add_months(value, months):
    return value + timedelta(days=30 * months)

def business_days(start, end):
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count

def is_leap_year(year):
    return year % 4 == 0

def next_weekday(value, weekday):
    return value + timedelta(days=(weekday - value.weekday()) % 7)

def format_duration(seconds):
    return f"{seconds}s"
