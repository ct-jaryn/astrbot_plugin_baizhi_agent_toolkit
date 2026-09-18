"""Streamable HTTP MCP client for the hosted Baizhi Agent Toolkit service.

This plugin speaks MCP directly to the hosted endpoint instead of copying any
server-side capability: the service stays authoritative, and the plugin only
carries the connection details and the user's own API key.

Design note — one session per tool call. A long-lived session is faster, but in
a long-running chat bot it introduces stale-session and half-closed-transport
failure modes that are hard to diagnose. Opening a session per call keeps the
failure mode honest ("this call failed") and guarantees the configured key is
the one actually used. Tool calls are user-triggered, so the extra handshake is
acceptable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

DEFAULT_ENDPOINT = "https://agent-toolkit.app.baizhi.cloud/mcp"

# Tools this plugin knows how to present. The hosted service may offer more;
# only tools listed here are registered so the model never sees a half-described
# capability.
KNOWN_TOOLS = ("websearch_search", "web_scrape", "web_extract")


class BaizhiToolError(RuntimeError):
    """Raised when the hosted service cannot be reached or rejects the request."""


def _load_sdk():
    """Import the MCP SDK lazily so a missing dependency surfaces as a readable
    tool error instead of breaking plugin loading.

    The transport was renamed across SDK generations: older builds export
    `streamablehttp_client` (which takes `headers` directly), current ones
    export `streamable_http_client` (which takes an injected HTTP client).
    AstrBot ships either, so accept both names and let `_open_streams` decide
    the calling convention from the signature.
    """
    try:
        import mcp
    except Exception as exc:  # noqa: BLE001 - report any import failure verbatim
        raise BaizhiToolError(
            "Python 'mcp' package is unavailable, so Baizhi tools cannot run. "
            f"Install it in the AstrBot environment (pip install mcp). Detail: {exc}"
        ) from exc

    transport = None
    try:
        from mcp.client.streamable_http import streamablehttp_client as transport  # legacy name
    except ImportError:
        try:
            from mcp.client.streamable_http import (  # current name
                streamable_http_client as transport,
            )
        except ImportError as exc:
            raise BaizhiToolError(
                "The installed 'mcp' package has no Streamable HTTP transport, "
                f"so Baizhi tools cannot run. Detail: {exc}"
            ) from exc

    if transport is None:  # pragma: no cover - defensive
        raise BaizhiToolError("Could not resolve an MCP Streamable HTTP transport from the installed 'mcp' package.")
    return mcp, transport


def _client_read_timeout(seconds: int):
    """Return the read timeout in the type this MCP SDK build expects.

    mcp 1.x declared `read_timeout_seconds: timedelta`, mcp 2.x declares
    `float`. Passing the wrong one fails deep inside the dispatcher with an
    opaque `TypeError` wrapped in an ExceptionGroup, so pick by annotation.
    """
    try:
        import inspect

        import mcp

        annotation = inspect.signature(mcp.ClientSession.__init__).parameters["read_timeout_seconds"].annotation
        if "timedelta" in str(annotation):
            return timedelta(seconds=seconds)
    except Exception:  # noqa: BLE001 - fall through to the modern form
        pass
    return float(seconds)


@asynccontextmanager
async def _open_streams(streamablehttp_client, url: str, headers: dict, read_timeout: timedelta) -> AsyncIterator:
    """Open the transport for either the legacy or the current SDK signature.

    The MCP SDK changed `streamablehttp_client` between releases: older versions
    accept `headers=`/timeouts directly, newer ones expect a configured
    `httpx.AsyncClient`. Which one is installed depends on the AstrBot
    environment, so support both by inspecting the signature.
    """
    import inspect

    params = inspect.signature(streamablehttp_client).parameters

    if "headers" in params:
        kwargs: dict[str, Any] = {"url": url, "headers": headers}
        if "timeout" in params:
            kwargs["timeout"] = read_timeout
        if "sse_read_timeout" in params:
            kwargs["sse_read_timeout"] = read_timeout
        if "terminate_on_close" in params:
            kwargs["terminate_on_close"] = True
        async with streamablehttp_client(**kwargs) as streams:
            yield streams
        return

    try:
        # mcp 2.x is built against httpx2; older builds use httpx. Import
        # whichever is present rather than pinning one, so the plugin keeps
        # working when AstrBot's dependency tree moves.
        try:
            import httpx2 as http_client_module
        except ImportError:
            import httpx as http_client_module
    except Exception as exc:  # noqa: BLE001
        raise BaizhiToolError(f"This MCP SDK build requires an HTTP client library that is unavailable. Detail: {exc}") from exc

    seconds = read_timeout.total_seconds()
    async with http_client_module.AsyncClient(
        headers=headers,
        timeout=http_client_module.Timeout(seconds, read=seconds),
        follow_redirects=True,
    ) as client:
        kwargs = {"url": url, "http_client": client}
        if "terminate_on_close" in params:
            kwargs["terminate_on_close"] = True
        async with streamablehttp_client(**kwargs) as streams:
            yield streams


async def probe(
    *,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: int = 30,
) -> tuple[bool, str]:
    """Initialize a session and list tools, without calling any billable tool.

    Used by the `/baizhi_check` command so a misconfigured key or endpoint is
diagnosed up front rather than on the first real tool call. Returns
    ``(ok, message)``; the message never contains the API key.
    """
    if not api_key:
        return False, "未配置 API Key。"

    read_timeout = timedelta(seconds=max(int(timeout_seconds), 1))
    mcp, streamablehttp_client = _load_sdk()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        async with _open_streams(
            streamablehttp_client,
            endpoint or DEFAULT_ENDPOINT,
            headers,
            read_timeout,
        ) as streams:
            async with mcp.ClientSession(
                streams[0], streams[1], read_timeout_seconds=_client_read_timeout(int(timeout_seconds))
            ) as session:
                await session.initialize()
                listed = await session.list_tools()
    except BaizhiToolError as exc:
        return False, str(exc)
    except asyncio.TimeoutError:
        return False, f"连接超时（{timeout_seconds}s）。"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    names = [getattr(tool, "name", "?") for tool in getattr(listed, "tools", None) or []]
    return True, f"连接成功，服务共返回 {len(names)} 个工具：{', '.join(names) or '(无)'}"


def _result_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into plain text for the model.

    Errors are returned as text rather than raised: AstrBot surfaces the return
    value to the model, and a readable message lets the model explain the
    problem or retry instead of failing the whole turn.
    """
    chunks: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
            continue
        # Non-text content (images, embedded resources) is summarised rather
        # than dumped, so a large payload cannot flood the conversation.
        item_type = getattr(item, "type", None) or type(item).__name__
        chunks.append(f"[{item_type} content omitted; this plugin returns text only]")

    structured = getattr(result, "structuredContent", None)
    if not chunks and structured is not None:
        chunks.append(str(structured))

    body = "\n".join(chunks).strip() or "(the service returned no content)"

    if getattr(result, "isError", False):
        return f"Baizhi tool reported an error: {body}"
    return body


async def call_tool(
    tool_name: str,
    arguments: dict,
    *,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: int = 60,
) -> str:
    """Call one tool on the hosted service and return its text result."""
    if not api_key:
        return (
            "Baizhi Agent Toolkit is not configured: no API key is set. "
            "Open the plugin settings, fill in the Baizhi Cloud API key and reload the plugin."
        )

    arguments = {k: v for k, v in arguments.items() if v is not None and v != ""}
    read_timeout = timedelta(seconds=max(int(timeout_seconds), 1))

    mcp, streamablehttp_client = _load_sdk()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        async with _open_streams(
            streamablehttp_client,
            endpoint or DEFAULT_ENDPOINT,
            headers,
            read_timeout,
        ) as streams:
            # mcp 1.x yields (read, write, get_session_id); mcp 2.x yields
            # (read, write). Take the first two explicitly, otherwise the third
            # lands in ClientSession's read_timeout_seconds position.
            async with mcp.ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=_client_read_timeout(int(timeout_seconds)),
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    name=tool_name,
                    arguments=arguments,
                    read_timeout_seconds=_client_read_timeout(int(timeout_seconds)),
                )
    except BaizhiToolError:
        raise
    except asyncio.TimeoutError:
        return f"Baizhi tool '{tool_name}' timed out after {timeout_seconds}s. The service may be slow or unreachable."
    except Exception as exc:  # noqa: BLE001 - never leak a traceback into chat
        # The exception text can echo request details; it never contains the key
        # because the key is only ever placed in the Authorization header.
        return f"Baizhi tool '{tool_name}' failed: {type(exc).__name__}: {exc}"

    return _result_to_text(result)
