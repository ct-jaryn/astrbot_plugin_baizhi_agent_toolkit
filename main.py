"""百智云 Agent Toolkit —— AstrBot 插件。

工具通过远程 MCP（Streamable HTTP）转发到百智云托管服务，插件本身不实现任何抓取能力。
"""

import copy

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .baizhi_client import DEFAULT_ENDPOINT, KNOWN_TOOLS, call_tool, probe

PLUGIN_NAME = "astrbot_plugin_baizhi_agent_toolkit"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO = "https://github.com/ct-jaryn/astrbot_plugin_baizhi_agent_toolkit"

_DISCLOSURE = "输入会发送到百智云托管服务，调用可能消耗服务额度。"

# 只在注册时给 LLM 看的参数名保持与 MCP 工具一致，转发时原样传给服务。
_TOOL_SPECS: dict[str, dict] = {
    "websearch_search": {
        "name": "baizhi_web_search",
        "description": (
            f"通过百智云 Agent Toolkit 搜索公开网页。{_DISCLOSURE}"
            "需要限定站点时，请使用 domains_json / exclude_domains_json 参数，不要在 query 里写 site: 语法。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词，不要包含 site: 过滤语法"},
                "count": {
                    "type": "integer",
                    "description": "返回结果数量，1 到 50，默认 10",
                    "minimum": 1,
                    "maximum": 50,
                },
                "time_range": {
                    "type": "string",
                    "description": "时间范围过滤，可选：day / week / month / year",
                    "enum": ["day", "week", "month", "year"],
                },
                "need_summary": {
                    "type": "boolean",
                    "description": "是否要求返回摘要，默认 false；仅在确有必要时开启",
                },
                "domains_json": {
                    "type": "string",
                    "description": '可选，限定域名或 IP 的 JSON 数组字符串，例如 ["example.com"]',
                },
                "exclude_domains_json": {
                    "type": "string",
                    "description": '可选，排除域名或 IP 的 JSON 数组字符串，例如 ["example.com"]',
                },
            },
            "required": ["query"],
        },
    },
    "web_scrape": {
        "name": "baizhi_web_scrape",
        "description": (
            f"通过百智云 Agent Toolkit 读取单个公开网页。{_DISCLOSURE}"
            "不要提交内网地址、带凭据的链接或敏感数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "一个公开的 HTTP 或 HTTPS 页面地址"},
                "accept_language": {"type": "string", "description": "可选，期望语言，例如 zh-CN 或 en-US"},
                "return_format": {
                    "type": "string",
                    "description": "返回格式，默认 markdown",
                    "enum": ["markdown", "json"],
                },
                "download": {
                    "type": "boolean",
                    "description": "是否生成可下载归档；仅在用户明确要求下载时开启，默认 false",
                },
            },
            "required": ["url"],
        },
    },
    "web_extract": {
        "name": "baizhi_web_extract",
        "description": (
            f"通过百智云 Agent Toolkit 从单个公开网页中按字段或指令提取信息。{_DISCLOSURE}"
            "fields_json 与 instruction 至少要提供一个。不要提交内网地址、带凭据的链接或敏感数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "一个公开的 HTTP 或 HTTPS 页面地址"},
                "accept_language": {"type": "string", "description": "可选，期望语言，例如 zh-CN 或 en-US"},
                "fields_json": {
                    "type": "string",
                    "description": '可选，字段名到类型的 JSON 对象字符串，例如 {"title":"string"}',
                },
                "instruction": {
                    "type": "string",
                    "description": "可选，描述要提取的内容；未提供 fields_json 时必填",
                },
                "download": {
                    "type": "boolean",
                    "description": "是否生成可下载归档；仅在用户明确要求下载时开启，默认 false",
                },
            },
            "required": ["url"],
        },
    },
}


def _parse_enabled_tools(raw) -> list[str]:
    """解析启用的工具；同时接受配置面板的列表与手改配置里的逗号分隔字符串。

    全部无法识别时回退到默认三项，避免手改配置把工具静默关光。
    """
    if raw is None:
        return list(KNOWN_TOOLS)
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(part).strip() for part in raw]
    else:
        return list(KNOWN_TOOLS)
    selected = [item for item in items if item in KNOWN_TOOLS]
    return selected or list(KNOWN_TOOLS)


@register(
    PLUGIN_NAME,
    "ct-jaryn",
    "百智云 Agent Toolkit 托管搜索与网页读取工具（自带 Key，可选工具，调用可能计费）",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class BaizhiAgentToolkitPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.plugin_config = dict(self.config)
        self.enabled_tools = _parse_enabled_tools(self.plugin_config.get("enabled_tools"))

        api_key_set = bool(self._api_key())
        if not api_key_set:
            # 仍然注册工具：调用时返回明确的配置提示，比"工具凭空消失"更容易排查。
            logger.warning(
                "百智云 Agent Toolkit：尚未配置 API Key，工具已注册但调用会提示先完成配置。"
            )

        self.tools = [self._build_tool(key) for key in self.enabled_tools]
        self.context.add_llm_tools(*self.tools)
        logger.info(
            f"百智云 Agent Toolkit {PLUGIN_VERSION} 已注册工具：{', '.join(self.enabled_tools) or '无'}；"
            f"端点 {self._endpoint()}；API Key {'已配置' if api_key_set else '未配置'}"
        )

    # --- 配置读取（每次调用都重新读，配置改动后无需重建工具）---

    def _api_key(self) -> str:
        return str(self.plugin_config.get("baizhi_api_key") or "").strip()

    def _endpoint(self) -> str:
        return str(self.plugin_config.get("endpoint") or DEFAULT_ENDPOINT).strip()

    def _timeout(self) -> int:
        try:
            return max(int(self.plugin_config.get("timeout_seconds") or 60), 1)
        except (TypeError, ValueError):
            return 60

    # --- 工具构造 ---

    def _build_tool(self, key: str) -> FunctionTool:
        """按上游约定构造工具：handler 优先于子类 call()。

        用 handler 而不是继承 FunctionTool，是因为该基类是 pydantic dataclass，
        普通子类的类属性默认值不会被识别为字段，name/description/parameters 会
        被判定为必填而抛 ValidationError。
        """
        spec = _TOOL_SPECS[key]
        plugin = self

        async def handler(event: AstrMessageEvent, **kwargs) -> str:
            # `key` is the MCP tool name on the hosted service; spec["name"] is
            # the AstrBot-facing name (baizhi_* prefix, to avoid colliding with
            # other plugins). Sending the prefixed name to the service would
            # fail with "tool not found".
            return await call_tool(
                key,
                dict(kwargs),
                api_key=plugin._api_key(),
                endpoint=plugin._endpoint(),
                timeout_seconds=plugin._timeout(),
            )

        return FunctionTool(
            name=spec["name"],
            description=spec["description"],
            parameters=copy.deepcopy(spec["parameters"]),
            handler=handler,
        )

    @filter.command("baizhi_check")
    async def baizhi_check(self, event: AstrMessageEvent):
        """检查百智云连接配置（仅 initialize 与 tools/list，不发起计费工具调用）。"""
        if not self._api_key():
            yield event.plain_result(
                "百智云 Agent Toolkit 尚未配置 API Key。请在插件配置中填写后重载插件。\n"
                f"端点：{self._endpoint()}\n已启用工具：{', '.join(self.enabled_tools)}"
            )
            return

        # 只做 initialize + tools/list：不产生计费调用，也不会回显 API Key。
        ok, message = await probe(api_key=self._api_key(), endpoint=self._endpoint())
        yield event.plain_result(
            f"{'连接正常。' if ok else '连接失败。'}\n"
            f"端点：{self._endpoint()}\n"
            f"已启用工具：{', '.join(self.enabled_tools)}\n{message}"
        )
