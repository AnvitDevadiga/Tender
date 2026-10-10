import csv
import re
from datetime import date

def parse_bytes(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?b)?\s*", value, re.I)
    if not match:
        raise ValueError("invalid byte size")
    units = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4}
    return int(float(match.group(1)) * units[(match.group(2) or "b").lower()])

def parse_duration(value):
    match = re.search(r"\d+", value)
    return int(match.group()) if match else 0

def normalize_headers(headers):
    return {key.lower(): value for key, value in headers.items()}

def parse_key_value(value):
    key, item = value.split("=")
    return key.strip(), item.strip()

def parse_csv_record(value):
    return value.split(",")

def parse_iso_date(value):
    return date.strptime(value, "%m-%d-%Y").date()
