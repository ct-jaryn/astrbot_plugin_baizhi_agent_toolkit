# 百智云 Agent Toolkit for AstrBot

把[百智云 Agent Toolkit](https://baizhi.cloud/landing/agent-toolkit) 的托管联网能力接进 AstrBot：
网页搜索、网页正文读取、按字段或指令的信息提取。

插件本身**不抓取任何网页**，也不复制服务端实现——它通过远程 MCP（Streamable HTTP）把工具调用转发给
百智云托管服务，使用**你自己的**百智云 API Key。

> 本插件不是 AstrBot 官方插件，也未获得 AstrBot 官方认证或背书。

## 前置条件

| 需要 | 说明 |
| --- | --- |
| AstrBot | `>=4.16,<5` |
| 百智云账号与 API Key | 在 <https://agent-toolkit.app.baizhi.cloud/> 创建 |
| 服务额度 | 工具调用会消耗你账号下的额度，**可能产生费用** |
| 网络 | 需要能访问 `agent-toolkit.app.baizhi.cloud` |

## 安装

**从插件市场安装**：在 AstrBot 管理面板的插件市场中搜索「百智云 Agent Toolkit」并安装。

**手动安装**：把本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_baizhi_agent_toolkit/`，
确保目录名为 `astrbot_plugin_baizhi_agent_toolkit`（AstrBot 以 Python 包的形式导入插件，
目录名含连字符会导入失败），然后重载插件。

Python 依赖 `mcp>=1.0.0` 与 `httpx>=0.24` 会自动安装。

## 配置

在管理面板的插件配置页填写：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `baizhi_api_key` | 密文输入 | 空 | 百智云 API Key。**只填 Key 本身，不要带 `Bearer ` 前缀** |
| `endpoint` | 字符串 | 百智云托管端点 | 仅在服务方调整地址时修改 |
| `enabled_tools` | 列表 | 三个工具全开 | 取消勾选的工具不会出现在 LLM 的工具列表里 |
| `timeout_seconds` | 整数 | `60` | 单次调用超时，网页读取与提取可能较慢 |

**关于 `secret` 必须说清楚的一点**：配置项的 `secret: true` 只在管理面板**遮罩显示**（并提供
临时显示按钮），**不会加密配置文件里的值**——Key 仍以明文保存在
`data/config/astrbot_plugin_baizhi_agent_toolkit_config.json`。本插件不会记录、回显或主动输出该 Key。

填好后**重载插件**，再发送 `/baizhi_check` 检查连通性。

## 工具

| 工具 | 作用 |
| --- | --- |
| `baizhi_web_search` | 搜索公开网页 |
| `baizhi_web_scrape` | 读取单个公开网页（markdown 或 json） |
| `baizhi_web_extract` | 按 `fields_json` 或 `instruction` 提取页面信息 |

三者都只接受**公开**页面地址。请勿提交内网地址、带凭据的链接或敏感数据。

搜索的站点过滤请用 `domains_json` / `exclude_domains_json` 参数（JSON 数组字符串），
**不要在 `query` 里写 `site:` 语法**。

## 指令

- `/baizhi_check` —— 检查 API Key 与端点是否可用。只做 `initialize` 与 `tools/list`，
  **不调用任何计费工具**，也不会输出或回显 API Key。

## 计费与数据

- 每次工具调用都会把请求发往百智云托管服务，并消耗你账号下的服务额度。
- 发送的内容包括：搜索词、目标 URL、提取字段或指令，以及必要的调用参数。
- 服务端对返回内容的留存策略以百智云的服务条款为准。
- `/baizhi_check` 只做初始化与工具列举，不产生计费调用。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 工具调用返回「未配置 API Key」 | 在插件配置中填入 Key 并重载插件 |
| `/baizhi_check` 报认证失败 | 检查 Key 是否填错、是否过期，以及是否误加了 `Bearer ` 前缀 |
| 调用超时 | 调大 `timeout_seconds`；确认机器能访问托管端点 |
| LLM 看不到工具 | 检查 `enabled_tools` 是否把三个工具都取消了勾选 |
| 提示 `mcp` 包不可用 | 在 AstrBot 环境中执行 `pip install "mcp>=1.0.0"` |

## 开发与测试

```bash
pip install -r requirements.txt pytest pytest-asyncio uvicorn jsonschema pyyaml
pytest tests/ -v
```

（`uvicorn`、`jsonschema`、`pyyaml` 仅供测试使用，插件运行本身不需要。）

测试分两部分，都不使用真实百智云账号、Key 或额度：

- `tests/test_client.py` —— 起一个本地 MCP 服务（三个工具 + Bearer 鉴权），验证传输接线、
  Authorization 头确实携带配置的 Key、空参数不会被当成实参转发、以及各条失败路径都返回可读信息
  而不是抛异常。
- `tests/test_plugin_module.py` —— 用上游 `astrbot` 定义的最小副本打桩，验证工具注册、配置解析、
  参数转发、启动日志不泄露 Key。

客户端代码同时兼容 `mcp` 1.x 与 2.x（两代 SDK 的传输函数名、`read_timeout_seconds` 类型、
传输返回的元组长度都不同），两个版本下测试均通过。

## 已知限制

- 未在真实百智云服务上用真实 Key 做端到端验收；本地测试用的是模拟服务。
- 每次工具调用新建一个 MCP 会话（而非复用长连接）：失败语义更清晰，代价是每次调用多一次握手。
- 只注册三个工具；服务端若提供更多能力，需要另行接入。
- `endpoint` 未做白名单校验，请勿填写来路不明的地址——插件会把你的 Key 以 Bearer 形式发给该地址。

## 许可证

MIT
