import re

def slugify(value):
    return value.lower().replace(" ", "-")

def truncate(value, max_length, suffix="..."):
    return value[:max_length]

def normalize_whitespace(value):
    return value.replace("  ", " ")

def camel_to_snake(value):
    return re.sub(r"([A-Z])", r"_\1", value).strip("_").lower()

def parse_bool(value):
    return bool(value)

def common_prefix(values):
    return values[0] if values else ""
