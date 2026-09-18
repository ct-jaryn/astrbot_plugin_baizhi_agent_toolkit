"""Tests for the plugin module itself (tool registration, config parsing).

AstrBot is not installed here, so `astrbot.*` is stubbed with copies of the real
upstream definitions. `FunctionTool` is reproduced verbatim (field order,
defaults and the JSON-Schema `model_validator` all matter), so these tests fail
the same way the real plugin would if a tool schema is wrong.

Run:  pytest tests/ -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
from pathlib import Path

import pytest
from pydantic import model_validator
from pydantic.dataclasses import dataclass

PLUGIN_DIR = Path(__file__).resolve().parents[1]

# The published plugin directory is `astrbot_plugin_baizhi_agent_toolkit`
# (underscores), because AstrBot imports plugins as Python packages. This
# checkout lives in a hyphenated archive folder, so expose it under the real
# name via a symlink before importing.
_LINK_ROOT = Path(tempfile.gettempdir()) / "astrbot-plugin-import-root"
_MODULE_NAME = "astrbot_plugin_baizhi_agent_toolkit"
_LINK_ROOT.mkdir(exist_ok=True)
_LINK = _LINK_ROOT / _MODULE_NAME
if not _LINK.exists():
    _LINK.symlink_to(PLUGIN_DIR, target_is_directory=True)


def _install_astrbot_stubs() -> None:
    """Install minimal `astrbot.*` modules modelled on the upstream source."""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    agent = types.ModuleType("astrbot.core.agent")
    agent.__path__ = []
    tool = types.ModuleType("astrbot.core.agent.tool")

    @dataclass
    class ToolSchema:
        """Copied from astrbot/core/agent/tool.py (field order is significant)."""

        name: str
        description: str
        parameters: dict

        @model_validator(mode="after")
        def validate_parameters(self):
            import jsonschema

            jsonschema.validate(self.parameters, jsonschema.Draft202012Validator.META_SCHEMA)
            return self

    @dataclass
    class FunctionTool(ToolSchema):
        handler: object = None
        handler_module_path: str | None = None
        active: bool = True
        is_background_task: bool = False

        async def call(self, context, **kwargs) -> str:
            raise NotImplementedError

    tool.ToolSchema = ToolSchema
    tool.FunctionTool = FunctionTool

    class _Logger:
        def __init__(self):
            self.records: list[tuple[str, str]] = []

        def _log(self, level, message):
            self.records.append((level, str(message)))

        def info(self, message, *a, **k):
            self._log("INFO", message)

        def warning(self, message, *a, **k):
            self._log("WARNING", message)

        def error(self, message, *a, **k):
            self._log("ERROR", message)

    logger = _Logger()

    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.FunctionTool = FunctionTool
    api.logger = logger

    api_event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class _Filter:
        def command(self, name):
            def decorator(fn):
                fn.__astrbot_command__ = name
                return fn

            return decorator

    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.filter = _Filter()

    api_star = types.ModuleType("astrbot.api.star")

    class Context:
        def __init__(self):
            self.registered_tools: list = []

        def add_llm_tools(self, *tools):
            self.registered_tools.extend(tools)

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        def decorator(cls):
            cls.__register_args__ = args
            return cls

        return decorator

    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register

    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.core": core,
            "astrbot.core.agent": agent,
            "astrbot.core.agent.tool": tool,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.api.star": api_star,
        }
    )


_install_astrbot_stubs()
sys.path.insert(0, str(_LINK_ROOT))


@pytest.fixture(scope="module")
def main_module():
    return importlib.import_module(f"{_MODULE_NAME}.main")


@pytest.fixture(scope="module")
def known_tools(main_module):
    from astrbot_plugin_baizhi_agent_toolkit.baizhi_client import KNOWN_TOOLS

    return list(KNOWN_TOOLS)


def _plugin(main_module, config=None):
    context = sys.modules["astrbot.api.star"].Context()
    instance = main_module.BaizhiAgentToolkitPlugin(context, config if config is not None else {"baizhi_api_key": "K"})
    return instance, context


# --- config parsing ---


def test_default_enabled_tools_are_all_three(main_module, known_tools):
    assert main_module._parse_enabled_tools(None) == known_tools
    assert main_module._parse_enabled_tools(known_tools) == known_tools


def test_comma_string_is_accepted(main_module):
    assert main_module._parse_enabled_tools("web_scrape") == ["web_scrape"]
    assert main_module._parse_enabled_tools("web_scrape, web_extract") == ["web_scrape", "web_extract"]


def test_unknown_entries_are_dropped_and_empty_falls_back(main_module, known_tools):
    assert main_module._parse_enabled_tools(["web_scrape", "nope"]) == ["web_scrape"]
    # An all-unknown selection must not silently disable every tool.
    assert main_module._parse_enabled_tools(["nope"]) == known_tools
    assert main_module._parse_enabled_tools([]) == known_tools


# --- tool definitions ---


@pytest.mark.parametrize(
    ("index", "expected_name", "required"),
    [
        (0, "baizhi_web_search", {"query"}),
        (1, "baizhi_web_scrape", {"url"}),
        (2, "baizhi_web_extract", {"url"}),
    ],
)
def test_tool_schemas_are_valid_and_complete(main_module, index, expected_name, required):
    _, context = _plugin(main_module)
    tool = context.registered_tools[index]

    assert tool.name == expected_name
    assert tool.description
    assert tool.handler is not None, "handler must be set: AstrBot prefers it over call()"
    # FunctionTool's model_validator already ran on construction; assert shape too.
    assert tool.parameters["type"] == "object"
    assert set(tool.parameters["required"]) == required
    for prop in tool.parameters["properties"].values():
        assert prop["type"] in {"string", "integer", "boolean", "number", "array", "object"}


def test_search_tool_exposes_domain_filters_not_site_syntax(main_module):
    _, context = _plugin(main_module)
    props = context.registered_tools[0].parameters["properties"]

    assert "domains_json" in props and "exclude_domains_json" in props
    assert "site:" in props["query"]["description"]


def test_tools_forward_config_and_arguments(main_module, monkeypatch):
    calls = []

    async def fake_call_tool(tool_name, arguments, **kwargs):
        calls.append((tool_name, arguments, kwargs))
        return "ok"

    monkeypatch.setattr(main_module, "call_tool", fake_call_tool)

    _, context = _plugin(
        main_module,
        {
            "baizhi_api_key": "  SECRET-KEY  ",
            "endpoint": "https://example.invalid/mcp",
            "timeout_seconds": 12,
        },
    )
    tool = context.registered_tools[0]

    result = _run(lambda: tool.handler(_Event(), query="hello", count=3))

    assert result == "ok"
    tool_name, arguments, kwargs = calls[0]
    # The hosted service expects the MCP tool name, not the baizhi_* alias.
    assert tool_name == "websearch_search"
    assert arguments == {"query": "hello", "count": 3}
    assert kwargs["api_key"] == "SECRET-KEY"  # whitespace stripped
    assert kwargs["endpoint"] == "https://example.invalid/mcp"
    assert kwargs["timeout_seconds"] == 12


def test_defaults_are_applied_when_config_is_sparse(main_module, monkeypatch):
    calls = []

    async def fake_call_tool(tool_name, arguments, **kwargs):
        calls.append((tool_name, arguments, kwargs))
        return "ok"

    monkeypatch.setattr(main_module, "call_tool", fake_call_tool)

    _, context = _plugin(main_module, {})
    tool = context.registered_tools[0]
    _run(lambda: tool.handler(_Event(), url="https://example.com"))

    _, _, kwargs = calls[0]
    assert kwargs["endpoint"].startswith("https://agent-toolkit.app.baizhi.cloud")
    assert kwargs["timeout_seconds"] == 60


def test_bad_timeout_value_falls_back_to_default(main_module, monkeypatch):
    calls = []

    async def fake_call_tool(tool_name, arguments, **kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(main_module, "call_tool", fake_call_tool)
    _, context = _plugin(main_module, {"baizhi_api_key": "K", "timeout_seconds": "not-a-number"})
    _run(lambda: context.registered_tools[0].handler(_Event(), query="x"))

    assert calls[0]["timeout_seconds"] == 60


# --- plugin wiring ---


def test_plugin_registers_only_enabled_tools(main_module):
    instance, context = _plugin(main_module, {"baizhi_api_key": "K", "enabled_tools": ["web_scrape"]})

    assert [t.name for t in context.registered_tools] == ["baizhi_web_scrape"]
    assert [t.name for t in instance.tools] == ["baizhi_web_scrape"]


def test_plugin_registers_all_by_default(main_module, known_tools):
    _, context = _plugin(main_module)

    assert [t.name for t in context.registered_tools] == [
        "baizhi_web_search",
        "baizhi_web_scrape",
        "baizhi_web_extract",
    ]


def test_plugin_still_registers_tools_without_a_key_but_warns(main_module):
    logger = sys.modules["astrbot.api"].logger
    logger.records.clear()

    _, context = _plugin(main_module, {})

    assert len(context.registered_tools) == 3
    assert any(level == "WARNING" and "API Key" in msg for level, msg in logger.records)


def test_startup_log_never_contains_the_key(main_module):
    logger = sys.modules["astrbot.api"].logger
    logger.records.clear()

    _plugin(main_module, {"baizhi_api_key": "SUPER-SECRET-VALUE"})

    assert logger.records, "expected a startup log line"
    for _, message in logger.records:
        assert "SUPER-SECRET-VALUE" not in message, message


def test_register_decorator_uses_the_published_identity(main_module, known_tools):
    args = main_module.BaizhiAgentToolkitPlugin.__register_args__
    assert args[0] == "astrbot_plugin_baizhi_agent_toolkit"
    # Must be the plugin's own repo (the market fetches the ZIP from here),
    # not the MCP server repo.
    assert args[4] == "https://github.com/ct-jaryn/astrbot_plugin_baizhi_agent_toolkit"


def test_metadata_matches_the_module_constants(main_module):
    """metadata.yaml is what the market parses; main.py is what AstrBot loads.

    Drift between the two would publish a listing pointing at the wrong repo or
    a wrong version, so assert they agree.
    """
    import yaml

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))

    assert meta["name"] == main_module.PLUGIN_NAME
    assert meta["version"] == main_module.PLUGIN_VERSION
    assert meta["repo"] == main_module.PLUGIN_REPO
    assert meta["name"] == _MODULE_NAME, "plugin dir / metadata name must match"


def test_market_text_fields_are_plain_text(main_module, known_tools):
    """The market card renders `desc`/`short_desc` as plain text.

    Verified against the live listing: `**bold**`, backticks and `-` bullets
    all appear literally, and paragraph breaks collapse to spaces. So these
    fields must read correctly with no Markdown applied.
    """
    import yaml

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))

    for field in ("desc", "short_desc"):
        text = meta.get(field) or ""
        assert "**" not in text, f"{field}: Markdown bold would show literally"
        assert "`" not in text, f"{field}: backticks would show literally"
        assert "\n  - " not in text, f"{field}: Markdown bullets would show literally"
        assert not any(line.strip().startswith("- ") for line in text.splitlines()), f"{field}: bullet list"


def test_market_category_is_a_valid_key(main_module):
    """The category must be one of the keys the market actually serves.

    Read live from https://cloud.astrbot.app/api/v1/market/categories. An
    unknown value would silently fall back to 其他 and hurt discoverability.
    """
    import yaml

    meta = yaml.safe_load((PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    valid = {"三方集成", "生活", "工具", "长期记忆", "知识库", "娱乐", "其他"}

    assert meta["category"] in valid, f"未知分类 {meta['category']!r}"


def test_config_schema_covers_every_option_the_code_reads(main_module):
    import json

    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))

    for key in ("baizhi_api_key", "endpoint", "enabled_tools", "timeout_seconds"):
        assert key in schema, f"{key} is read by the plugin but missing from _conf_schema.json"

    # The API key must be masked in the panel.
    assert schema["baizhi_api_key"]["secret"] is True

    # The declared defaults must be tools this plugin actually knows.
    from astrbot_plugin_baizhi_agent_toolkit.baizhi_client import KNOWN_TOOLS

    assert set(schema["enabled_tools"]["default"]) <= set(KNOWN_TOOLS)


# --- helpers ---


class _Event:
    """Stand-in for AstrMessageEvent; the handler only forwards kwargs."""


def _run(coro_factory):
    import anyio

    return anyio.run(coro_factory)
