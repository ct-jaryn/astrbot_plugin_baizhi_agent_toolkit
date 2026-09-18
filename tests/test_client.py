"""End-to-end tests for the Baizhi MCP client.

These run against a local MCP server that mimics the hosted service's shape
(three tools, Bearer auth). No real Baizhi endpoint, key or credit is used —
the point is to prove the transport wiring, the auth header, and every error
path without touching production.

Run:  pytest tests/ -v
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from baizhi_client import DEFAULT_ENDPOINT, call_tool, probe  # noqa: E402

DUMMY_KEY = "DUMMY-KEY-FOR-TESTS-0000"
EXPECTED_HEADER = f"Bearer {DUMMY_KEY}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeService:
    """A minimal MCP server that behaves like the hosted service."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.received_auth: list[str | None] = []
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        import uvicorn

        # mcp 2.x renamed FastMCP to MCPServer; support both so this test can
        # exercise the legacy client path as well as the current one.
        try:
            from mcp.server.mcpserver import MCPServer as ServerClass
        except ImportError:
            from mcp.server.fastmcp import FastMCP as ServerClass

        server = ServerClass("fake-baizhi")

        @server.tool(name="websearch_search")
        def websearch_search(query: str, count: int = 10) -> str:
            """Search the web."""
            return f"search:{query}:{count}"

        @server.tool(name="web_scrape")
        def web_scrape(url: str) -> str:
            """Read a page."""
            return f"scrape:{url}"

        @server.tool(name="web_extract")
        def web_extract(url: str, instruction: str = "") -> str:
            """Extract fields from a page."""
            return f"extract:{url}:{instruction}"

        app = server.streamable_http_app()

        # Middleware records the Authorization header so the test can prove the
        # user's key is what actually reaches the service.
        received = self.received_auth

        async def record_auth(scope, receive, send):
            if scope["type"] == "http":
                for name, value in scope.get("headers") or []:
                    if name == b"authorization":
                        received.append(value.decode())
            await app(scope, receive, send)

        config = uvicorn.Config(record_auth, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("fake MCP service did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def service():
    fake = FakeService()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


def test_call_tool_round_trip(service):
    result = call_tool_sync(
        "websearch_search",
        {"query": "baizhi", "count": 3},
        api_key=DUMMY_KEY,
        endpoint=service.url,
    )
    assert result == "search:baizhi:3", result


def test_authorization_header_carries_the_configured_key(service):
    service.received_auth.clear()
    call_tool_sync("web_scrape", {"url": "https://example.com"}, api_key=DUMMY_KEY, endpoint=service.url)
    assert service.received_auth, "no request reached the service"
    assert set(service.received_auth) == {EXPECTED_HEADER}, service.received_auth


def test_empty_arguments_are_dropped(service):
    # An empty string must not be forwarded as a real argument value.
    result = call_tool_sync(
        "web_extract",
        {"url": "https://example.com", "instruction": ""},
        api_key=DUMMY_KEY,
        endpoint=service.url,
    )
    assert result == "extract:https://example.com:", result


def test_probe_lists_tools_without_calling_any(service):
    ok, message = probe_sync(api_key=DUMMY_KEY, endpoint=service.url)
    assert ok is True, message
    assert "3 个工具" in message, message
    for name in ("websearch_search", "web_scrape", "web_extract"):
        assert name in message, message


def test_missing_key_is_reported_not_raised(service):
    result = call_tool_sync("websearch_search", {"query": "x"}, api_key="", endpoint=service.url)
    assert "未配置" in result or "not configured" in result, result
    assert DUMMY_KEY not in result


def test_reachable_but_invalid_endpoint_fails_cleanly():
    # Nothing is listening here: the call must return a message, never raise.
    result = call_tool_sync("websearch_search", {"query": "x"}, api_key=DUMMY_KEY, endpoint="http://127.0.0.1:1/mcp")
    assert "failed" in result or "timed out" in result, result
    assert DUMMY_KEY not in result, "the key must never be echoed back"


def test_key_never_appears_in_results(service):
    for tool, args in (
        ("websearch_search", {"query": "q"}),
        ("web_scrape", {"url": "https://example.com"}),
    ):
        result = call_tool_sync(tool, args, api_key=DUMMY_KEY, endpoint=service.url)
        assert DUMMY_KEY not in result, result


# --- tiny sync wrappers: the plugin is async, these tests are not ---


def call_tool_sync(tool, args, **kwargs):
    import anyio

    return anyio.run(lambda: call_tool(tool, args, **kwargs))


def probe_sync(**kwargs):
    import anyio

    return anyio.run(lambda: probe(**kwargs))


def test_default_endpoint_is_the_hosted_service():
    assert DEFAULT_ENDPOINT == "https://agent-toolkit.app.baizhi.cloud/mcp"
