"""Bounded hosted MCP client. No tool call is automatically retried.

Supports the modern transport API in MCP 1.30+ and MCP 2.x. Result attributes
and read timeout types differ between those SDK generations.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
import importlib
import inspect
import json
import logging
from typing import Any

import anyio

try:
    from builtins import BaseExceptionGroup
except ImportError:  # Python 3.10, also supported by the MCP dependency range.
    from exceptiongroup import BaseExceptionGroup

if __package__:
    from .baizhi_arguments import ALLOWED_TOOLS, InputError, build_arguments
else:  # Direct offline client tests.
    from baizhi_arguments import ALLOWED_TOOLS, InputError, build_arguments

DEFAULT_ENDPOINT = "https://agent-toolkit.app.baizhi.cloud/mcp"
KNOWN_TOOLS = ALLOWED_TOOLS
MAX_RESULT_CHARS = 2_000_000
MAX_DISCOVERY_PAGES = 10


class BaizhiToolError(RuntimeError):
    """Transport/configuration failure; its raw text is never returned."""


class BaizhiDependencyError(BaizhiToolError):
    pass


# AstrBot is a shared process: suppress upstream diagnostics only in this
# client's async context, including child MCP tasks; leave other clients alone.
_private_exchange = ContextVar("baizhi_private_exchange", default=False)


class _PrivateDiagnostics(logging.Filter):
    def filter(self, record):
        return not _private_exchange.get()


for _name in (
    "mcp.client.streamable_http", "client", "mcp.shared.jsonrpc_dispatcher",
    "mcp.shared.dispatcher", "mcp.shared.session", "mcp.shared.tool_name_validation",
    "httpx", "httpx2", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks", "httpcore2.connection", "httpcore2.http11",
    "httpcore2.http2", "httpcore2.proxy", "httpcore2.socks",
):
    logging.getLogger(_name).addFilter(_PrivateDiagnostics())


def _load_sdk():
    try:
        import mcp
        from mcp.client.streamable_http import streamable_http_client
        return mcp, streamable_http_client
    except Exception:
        raise BaizhiDependencyError() from None


def _client_read_timeout(seconds: int):
    import mcp
    annotation = inspect.signature(mcp.ClientSession.__init__).parameters["read_timeout_seconds"].annotation
    return timedelta(seconds=seconds) if "timedelta" in str(annotation) else float(seconds)


def _configuration(api_key, endpoint, timeout_seconds):
    if not isinstance(api_key, str):
        raise BaizhiToolError()
    key = api_key.strip()
    if not key or len(key) > 512 or key.lower().startswith("bearer ") or any(not 33 <= ord(c) <= 126 for c in key):
        raise BaizhiToolError()
    # Legacy endpoint setting is accepted only at the fixed hosted destination.
    if endpoint != DEFAULT_ENDPOINT:
        raise BaizhiToolError()
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise BaizhiToolError()
    return key, timeout_seconds


async def _guard_request(request):
    if str(request.url) != DEFAULT_ENDPOINT or request.method not in {"POST", "GET", "DELETE"}:
        raise BaizhiToolError()


async def _guard_response(response):
    # Also precedes the SDK's own same-origin redirect handling.
    if 300 <= response.status_code < 400:
        raise BaizhiToolError()
    if response.status_code >= 400 and not (
        response.status_code == 405 and response.request.method in {"GET", "DELETE"}
    ):
        raise BaizhiToolError()


@asynccontextmanager
async def _open_streams(transport, url, headers, read_timeout):
    annotation = inspect.signature(transport).parameters["http_client"].annotation
    module_name = "httpx2" if "httpx2" in str(annotation) else "httpx"
    try:
        http = importlib.import_module(module_name)
    except Exception:
        raise BaizhiDependencyError() from None
    seconds = read_timeout.total_seconds()
    async with http.AsyncClient(
        headers=headers, timeout=http.Timeout(seconds), follow_redirects=False,
        trust_env=False, event_hooks={"request": [_guard_request], "response": [_guard_response]},
    ) as client:
        async with transport(url, http_client=client, terminate_on_close=True) as streams:
            yield streams


def _safe_failure(exc):
    # Only locally defined categories are exposed; no repr, traceback or body.
    if isinstance(exc, InputError):
        return "Baizhi input validation failed. Check the tool parameters and use a public HTTP(S) URL."
    if isinstance(exc, BaizhiDependencyError):
        return "Baizhi MCP dependency is unavailable. Install requirements.txt in the AstrBot environment."
    if isinstance(exc, TimeoutError):
        return "Baizhi request timed out. No automatic tool-call retry was made."
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            if isinstance(child, (TimeoutError, BaizhiDependencyError, BaseExceptionGroup)):
                message = _safe_failure(child)
                if message != "Baizhi connection or protocol validation failed. No automatic tool-call retry was made.":
                    return message
    return "Baizhi connection or protocol validation failed. No automatic tool-call retry was made."


def _redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


def _attribute(value, modern, legacy, default=None):
    return getattr(value, modern, getattr(value, legacy, default))


def _result_to_text(result: Any, api_key: str = "") -> str:
    if _attribute(result, "is_error", "isError", False):
        return "Baizhi tool reported an error. No automatic tool-call retry was made."
    chunks = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        chunks.append(text if isinstance(text, str) else "[non-text content omitted]")
    structured = _attribute(result, "structured_content", "structuredContent")
    if structured is not None:
        chunks.append(json.dumps(_redact(structured, api_key), ensure_ascii=False))
    body = "\n".join(chunks).strip() or "(the service returned no content)"
    if len(body) > MAX_RESULT_CHARS:
        return "Baizhi tool result exceeds the output limit."
    safe = _redact(body, api_key)
    if len(safe) > MAX_RESULT_CHARS:
        return "Baizhi tool result exceeds the output limit."
    return safe


async def _discard_notification(*args, **kwargs):
    return None


async def _discover(session, mcp):
    names = set()
    cursor = None
    seen = set()
    for _ in range(MAX_DISCOVERY_PAGES):
        page = await session.list_tools(params=mcp.types.PaginatedRequestParams(cursor=cursor) if cursor else None)
        for tool in page.tools:
            if tool.name in KNOWN_TOOLS:
                if tool.name in names:
                    raise BaizhiToolError()
                names.add(tool.name)
        cursor = _attribute(page, "next_cursor", "nextCursor")
        if not cursor:
            return names
        if cursor in seen:
            raise BaizhiToolError()
        seen.add(cursor)
    raise BaizhiToolError()


async def _exchange(tool_name, arguments, api_key, endpoint, timeout_seconds):
    with anyio.fail_after(timeout_seconds):
        mcp, transport = _load_sdk()
        async with _open_streams(transport, endpoint, {"Authorization": f"Bearer {api_key}"},
                                 timedelta(seconds=timeout_seconds)) as streams:
            async with mcp.ClientSession(
                streams[0], streams[1], read_timeout_seconds=_client_read_timeout(timeout_seconds),
                logging_callback=_discard_notification, message_handler=_discard_notification,
            ) as session:
                await session.initialize()
                names = await _discover(session, mcp)
                if tool_name is None:
                    if not names:
                        raise BaizhiToolError()
                    return names
                if tool_name not in names:
                    raise BaizhiToolError()
                return await session.call_tool(tool_name, arguments=arguments,
                                               read_timeout_seconds=_client_read_timeout(timeout_seconds))


async def probe(*, api_key: str, endpoint: str = DEFAULT_ENDPOINT, timeout_seconds: int = 30):
    """Initialize and discover only; the hosted service decides any billing policy."""
    if not api_key:
        return False, "未配置 API Key。"
    token = _private_exchange.set(True)
    try:
        key, timeout = _configuration(api_key, endpoint, timeout_seconds)
        names = await _exchange(None, None, key, endpoint, timeout)
        return True, f"连接成功，发现 {len(names)} 个支持的工具：{', '.join(n for n in KNOWN_TOOLS if n in names)}"
    except Exception as exc:
        return False, _safe_failure(exc)
    finally:
        _private_exchange.reset(token)


async def call_tool(tool_name: str, arguments: dict, *, api_key: str,
                    endpoint: str = DEFAULT_ENDPOINT, timeout_seconds: int = 60) -> str:
    if not api_key:
        return "Baizhi Agent Toolkit is not configured: no API key is set. Configure the plugin and reload it."
    token = _private_exchange.set(True)
    try:
        key, timeout = _configuration(api_key, endpoint, timeout_seconds)
        mapped = build_arguments(tool_name, arguments)
        result = await _exchange(tool_name, mapped, key, endpoint, timeout)
        return _result_to_text(result, key)
    except Exception as exc:
        return _safe_failure(exc)
    finally:
        _private_exchange.reset(token)
