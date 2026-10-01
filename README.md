# 百智云 Agent Toolkit for AstrBot

源码版本为 **1.0.3**。它修复 1.0.2 的参数映射、密钥回显路径、MCP 2 结果解析、空工具列表、错误处理，以及真实宿主绑定插件实例后工具 handler 参数不匹配的问题。市场下载是否包含修复，需核对条目的实际版本与 commit；旧 1.0.2 不包含这些修复。

插件通过远程 MCP（Streamable HTTP）连接[百智云托管服务](https://agent-toolkit.app.baizhi.cloud/)，使用用户自己的 API Key。它不实现抓取后端，不代表 AstrBot 官方认证。

## 运行前提与安装

- AstrBot `>=4.16,<5`；按宿主的 Python 约束安装。已检查的宿主 `pyproject.toml` 要求 Python 3.12 或以上，本次使用 Python 3.12.14；其他解释器及未来宿主版本未逐项验收。
- MCP SDK `>=1.30.0,<3`。客户端独立套件分别测试 1.30.0 和 2.2.0；真实 AstrBot 宿主使用其声明允许的 MCP 1.30.0（宿主要求 `<2`）。MCP 2 的客户端测试不代表宿主支持 MCP 2，不应绕过宿主的依赖约束。
- 账号、专用可撤销 API Key 和服务额度。工具输入会发送到百智云，调用可能消耗额度并产生费用。

先在隔离的 AstrBot 环境安装：可从已核对版本的市场条目安装，或使用宿主的插件 ZIP 上传入口；也可将本目录复制为 `data/plugins/astrbot_plugin_baizhi_agent_toolkit/`，安装 `requirements.txt` 后加载。不要把未经确认的升级直接覆盖到生产机器人。

## 配置

| 配置项 | 默认值 | 行为 |
| --- | --- | --- |
| `baizhi_api_key` | 空 | 只填 Key，不带 Bearer 前缀；空值不会发请求 |
| `endpoint` | 固定百智云 HTTPS 地址 | 为兼容旧配置保留字段；本候选只接受 `https://agent-toolkit.app.baizhi.cloud/mcp`，其他值在发送凭据前拒绝 |
| `enabled_tools` | 三个工具 | 未设置时默认三项；显式空列表关闭全部；未知项忽略并告警；重复项去重 |
| `timeout_seconds` | 60 | 握手、分页发现与工具调用合计的总超时；配置限制为 1–300 秒 |

保存配置后重载插件。`secret: true` **只遮罩管理面板，不加密配置文件**。Key 仍由 AstrBot 原样保存在插件配置文件中；控制该文件和备份的访问权限。

## 三个工具与参数

| AstrBot 名称 | MCP 工具 | 参数说明 |
| --- | --- | --- |
| `baizhi_web_search` | `websearch_search` | query、整数 count 1–50、time_range、need_summary；domains_json / exclude_domains_json 解码并映射为 filter 对象 |
| `baizhi_web_scrape` | `web_scrape` | URL、语言、markdown/json 格式；download 默认 false |
| `baizhi_web_extract` | `web_extract` | URL；fields_json 解码为 fields 对象；fields 或 instruction 至少提供一项；download 默认 false |

站点限定使用 JSON 数组，例如 `["example.com"]`，不要把 site: 拼入 query。字段使用 JSON 对象，例如 `{"title":"string","price":"number"}`，支持 string、number、boolean、array。未知参数和任意工具名会在联网前拒绝。映射依据仓库保留的 **2026-09-16 历史 schema**，不是当前生产服务 schema 验收。

输入校验拒绝非 HTTP(S)、明显私网 IP/本地主机、带凭据的链接、fragment、控制字符和非标准端口。**不解析 DNS，也不检查百智云远端抓取的跳转链**；不是完整 SSRF 防护。服务端仍须负责其抓取边界。不要发送私密链接、个人信息或敏感组织数据。

这些工具不能保证只读：搜索摘要可能被服务记录；`download=true` 可能创建远端下载归档。只有用户明确要求下载时才应开启。费用、留存、地域和删除策略以服务方实际政策为准。

## 连通性检查与失败处理

`/baizhi_check` 只执行 MCP 初始化和有界分页发现，不调用 tools/call；它只能说明连接及受支持工具是否可见，不能证明抓取/提取或计费策略已经验收。若只看到部分工具，会报告其可见的支持工具集合。

客户端禁止重定向和继承环境代理，只将凭据发到固定端点。**与 1.0.2 的兼容性变化：自定义 endpoint 和 HTTP(S)_PROXY/ALL_PROXY 等环境代理配置不再生效；依赖企业代理访问的部署需先验证直连路径，本候选没有代理配置入口。** 错误消息不带上游异常正文；正常结果中的当前 Key 精确回显会被脱敏，结构化 key/value 也会处理。MCP 与 HTTP 的已知诊断日志在本次调用上下文内被抑制，避免请求头/响应诊断回显；不全局关闭其他插件日志。这不是通用 PII 检测，也不证明 AstrBot 自身的日志、存储、导出或全部依赖没有泄露风险。

MCP 1 与 2 的结构化结果及错误状态分别适配。工具错误明确返回失败文字，不自动进行应用层 tools/call 重试。调用取消会传递给客户端；无法保证已开始的远端工作停止或不计费。结果输出上限为 2,000,000 字符，检查发生在 SDK 解析以后，不能限制传输量或峰值解析内存。

## 可复现的离线测试

使用专用环境，不需要真实 Key、服务账号或网络服务端：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/
```

安装依赖需要访问包索引；测试本身不联网。`tests/test_client.py` 使用真实 MCP SDK 和内存 HTTP transport，autouse fixture 阻止真实 HTTP transport。假服务会拒绝错误合成 Key，并验证实际 JSON-RPC 参数契约。测试覆盖三工具映射、URL/参数拒绝、redirect/HTTP/MCP 错误、Key 回显、两代结果字段、SDK/HTTP 库缺失、超时取消、分页与输出限制。

`tests/test_plugin_module.py` 使用 AstrBot API 的最小 stub 验证注册、空列表、去重、配置、日志及命令。测试会模拟宿主的 `functools.partial(handler, instance)` 绑定，再传消息事件；仅直接调用未绑定的闭包会漏掉真实执行错误。stub 本身不能证明宿主生命周期兼容。

2026-10-01 在官方稳定 AstrBot [4.28.2 / 3c7adafa](https://github.com/AstrBotDevs/AstrBot/commit/3c7adafa1397e182d60b1016bf88759265113c8a) 和 [4.28.1 / ab42c0d9](https://github.com/AstrBotDevs/AstrBot/commit/ab42c0d9b726d82ad0f9563e04c53a4460c00d61)、Python 3.12.14、MCP 1.30.0 的专用环境，各通过 30 项隔离宿主检查。实际 `PluginManager.install_plugin_from_file` 安装旧 1.0.2 源码的 ZIP，再安装内容与 digest 不同的修复 ZIP；真实配置文件路径、插件作用域与合成 Key 保留，配置编辑/重载和空列表关闭仍生效。旧版三工具均在实例/事件绑定处报 TypeError，修复版经原生 `FunctionToolExecutor` 和权限 wrapper 进入客户端与 SDK。

同一环境使用实际 `ToolLoopAgentRunner`，合成 Provider 先选择三个已安装工具，再接收三个工具结果并结束回合；不是直接调用 handler 代替工具调度。权限拒绝、输入错误、401、工具错误、文本/结构化 Key 回显、取消无重试、卸载清理与日志检查也通过。当前客户端及 API stub 单测在 MCP 1.30.0 和 2.2.0 各通过 79 项。

最低声明版本官方 [AstrBot 4.16.0 / fcd18503](https://github.com/AstrBotDevs/AstrBot/commit/fcd18503cbd59dab5883a9c96c7411547c978c55) 另通过 14 项最小兼容检查：真实 ZIP 安装、注册、插件实例绑定、原生 executor 调度三个 SDK 调用、参数映射、空 Key、配置保存/重载、空工具与卸载。该宿主使用原生 partial 绑定，未声称它具备新版的全局工具权限 wrapper。

这些检查只替换远端 HTTP 响应为 `httpx.MockTransport`；完整回合的模型响应由合成 Provider 提供。`ASTRBOT_ROOT`、SQLite、插件配置与 `TMPDIR` 均隔离，禁用指标上传，测试进程禁止网络出站，pip 禁用索引；没有真实模型或生产服务调用。管理器 ZIP 安装已验，WebUI 浏览器操作、消息平台命令分发和生产验收仍未覆盖。普通 stub 单测不能替代以上宿主检查。

## 验证边界与部署前检查

1. 离线宿主检查不覆盖 WebUI 浏览器交互、消息平台命令分发、真实 LLM provider 或多插件部署；其他宿主版本需另验。ZIP 安装入口与工具回合的合成验证见上文，不能当作真实模型/服务验收。
2. 部署时用已授权、专用可撤销 Key 核对现网 schema/权限，并在明确额度边界内验证所需工具。仓库保留的历史 schema 与合成测试不能代替这一步。
3. OS 凭据保护、备份/导出与访问权限、网络/TLS/代理部署、并发和响应内存边界验收。本次保存重载仅证明配置值能持久化并生效，不证明加密或 OS 隔离。
4. 按部署环境完成依赖与权限审查；升级后核验已安装版本和实际工具。发布者须另外核验市场版本、commit 与下载包，源码提交不等于市场更新。

这些部署边界不因离线检查通过而消失；本说明不宣称真实服务、费用或市场审核已验收。

MIT；见 LICENSE。
