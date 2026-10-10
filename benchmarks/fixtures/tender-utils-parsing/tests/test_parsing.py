import pytest
from datetime import date
from tender_utils.parsing import parse_bytes, parse_duration, normalize_headers, parse_key_value, parse_csv_record, parse_iso_date

def test_parse_bytes():
    assert parse_bytes("1 KB") == 1024
    assert parse_bytes("1.5 MB") == 1572864
    assert parse_bytes("2gb") == 2 * 1024**3

def test_parse_duration():
    assert parse_duration("1h 30m 5s") == 5405
    assert parse_duration("1.5h") == 5400
    with pytest.raises(ValueError):
        parse_duration("unknown")

def test_normalize_headers():
    assert normalize_headers({" X-Request ID ": "abc", "CONTENT-TYPE": "json"}) == {"x_request_id": "abc", "content_type": "json"}

def test_parse_key_value():
    assert parse_key_value(" token = a=b=c ") == ("token", "a=b=c")
    with pytest.raises(ValueError):
        parse_key_value("missing")

def test_parse_csv_record():
    assert parse_csv_record('one,"two,too","say ""hi"""') == ["one", "two,too", 'say "hi"']

def test_parse_iso_date():
    assert parse_iso_date("2024-02-29").isoformat() == "2024-02-29"
    with pytest.raises(ValueError):
        parse_iso_date("2023-02-29")
