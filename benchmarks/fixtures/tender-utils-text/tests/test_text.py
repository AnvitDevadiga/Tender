import pytest
from datetime import date
from tender_utils.text import slugify, truncate, normalize_whitespace, camel_to_snake, parse_bool, common_prefix

def test_slugify():
    assert slugify("  Crème brûlée, API!  ") == "creme-brulee-api"
    assert slugify("hello___world") == "hello-world"

def test_truncate():
    assert truncate("short", 8) == "short"
    assert truncate("abcdefgh", 6) == "abc..."
    with pytest.raises(ValueError):
        truncate("abc", 2)

def test_normalize_whitespace():
    assert normalize_whitespace("  alpha\t beta\n gamma  ") == "alpha beta gamma"

def test_camel_to_snake():
    assert camel_to_snake("HTTPServerID") == "http_server_id"
    assert camel_to_snake("parseJSONValue") == "parse_json_value"

def test_parse_bool():
    assert parse_bool(" YES ") is True
    assert parse_bool("false") is False
    assert parse_bool(True) is True
    with pytest.raises(ValueError):
        parse_bool("maybe")

def test_common_prefix():
    assert common_prefix(["tender/api", "tender/app", "tender/assets"]) == "tender/a"
    assert common_prefix([]) == ""
    assert common_prefix(["solo"]) == "solo"
