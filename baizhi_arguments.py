"""AstrBot arguments mapped to the historical MCP schema.

JSON text is used for nested fields because model-facing plugin parameters differ from MCP
JSON Schema. Validation errors never interpolate user input or credentials.
"""

import ipaddress
import json
import re
from urllib.parse import urlsplit

ALLOWED_TOOLS = ("websearch_search", "web_scrape", "web_extract")


class InputError(ValueError):
    pass


def _text(value, *, required=False, limit=10000):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise InputError("A text parameter is invalid or too long.")
    value = value.strip()
    if required and not value:
        raise InputError("A required text parameter is missing.")
    return value or None


def _boolean(value, default=False):
    if value is None:
        return default
    if type(value) is not bool:
        raise InputError("A boolean parameter must be true or false.")
    return value


def _json_field(value, expected_type):
    value = _text(value, limit=20000)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise InputError("A JSON parameter is malformed.") from None
    if not isinstance(parsed, expected_type):
        raise InputError("A JSON parameter has the wrong container type.")
    return parsed


def _domain(value):
    value = _text(value, required=True, limit=253)
    if any(c in value for c in "/?#@\\\r\n\t ") or "://" in value:
        raise InputError("Search filters must contain bare domains or IP addresses.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if "." not in value or ":" in value:
            raise InputError("Search filters must contain bare domains or IP addresses.") from None
    return value


def _url(value):
    value = _text(value, required=True, limit=8192)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname.rstrip(".") if parsed.hostname else None
        port = parsed.port
    except ValueError:
        raise InputError("Provide a public HTTP or HTTPS URL.") from None
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or "#" in value or "\\" in value
            or any(ord(c) <= 32 for c in value)):
        raise InputError("Provide a public HTTP or HTTPS URL without credentials or a fragment.")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise InputError("Local and private targets are not supported by this integration.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*", host):
            raise InputError("Provide a public hostname.") from None
    else:
        if not address.is_global:
            raise InputError("Local and private targets are not supported by this integration.")
    if port not in {None, 80, 443}:
        raise InputError("Only standard HTTP and HTTPS ports are supported by this integration.")
    return value


def build_arguments(name: str, parameters: dict) -> dict:
    if name not in ALLOWED_TOOLS or not isinstance(parameters, dict):
        raise InputError("This tool is not available in the integration.")
    allowed = {
        "websearch_search": {"query", "count", "need_summary", "time_range", "domains_json", "exclude_domains_json"},
        "web_scrape": {"url", "accept_language", "download", "return_format"},
        "web_extract": {"url", "accept_language", "download", "fields_json", "instruction"},
    }[name]
    if set(parameters) - allowed:
        raise InputError("Unsupported tool parameters were supplied.")
    if name == "websearch_search":
        count = parameters.get("count", 10)
        if type(count) not in {int, float} or not 1 <= count <= 50 or not float(count).is_integer():
            raise InputError("Result count must be an integer from 1 to 50.")
        time_range = parameters.get("time_range", "month")
        if not isinstance(time_range, str) or time_range not in {"day", "week", "month", "year"}:
            raise InputError("Select a supported search time range.")
        result = {"query": _text(parameters.get("query"), required=True), "count": int(count),
                  "need_summary": _boolean(parameters.get("need_summary")), "time_range": time_range}
        filters = {}
        for ui, remote in (("domains_json", "domains"), ("exclude_domains_json", "exclude_domains")):
            values = _json_field(parameters.get(ui), list)
            if values is not None:
                if len(values) > 50:
                    raise InputError("Use at most 50 domains in each search filter.")
                filters[remote] = [_domain(v) for v in values]
        if filters:
            result["filter"] = filters
        return result
    result = {"url": _url(parameters.get("url")), "download": _boolean(parameters.get("download"))}
    language = _text(parameters.get("accept_language"), limit=100)
    if language:
        if any(ord(c) < 32 for c in language):
            raise InputError("Language must not contain control characters.")
        result["accept_language"] = language
    if name == "web_scrape":
        format_value = parameters.get("return_format", "markdown")
        if not isinstance(format_value, str) or format_value not in {"markdown", "json"}:
            raise InputError("Select markdown or json for the page format.")
        result["return_format"] = format_value
        return result
    fields = _json_field(parameters.get("fields_json"), dict)
    instruction = _text(parameters.get("instruction"))
    if not fields and not instruction:
        raise InputError("Provide extraction fields or an instruction.")
    if fields:
        if len(fields) > 50 or any(not k.strip() or len(k) > 200 or not isinstance(v, str) or v not in {"string", "number", "boolean", "array"}
                                  for k, v in fields.items()):
            raise InputError("Extraction fields must map names to string, number, boolean, or array.")
        result["fields"] = fields
    if instruction:
        result["instruction"] = instruction
    return result
