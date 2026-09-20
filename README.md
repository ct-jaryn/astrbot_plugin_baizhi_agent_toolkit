# 百智云 Agent Toolkit for AstrBot

本目录是 **1.0.3-rc.1 本地修复候选，尚未发布**。它修复公开 1.0.2 的参数映射、密钥回显路径、MCP 2 结果解析、空工具列表、错误处理，以及真实宿主绑定插件实例后工具 handler 参数不匹配的问题。不要把市场中的 1.0.2 当作已包含这些修复；本次没有上传或更新市场。

插件通过远程 MCP（Streamable HTTP）连接[百智云托管服务](https://agent-toolkit.app.baizhi.cloud/)，使用用户自己的 API Key。它不实现抓取后端，不代表 AstrBot 官方认证。

## 运行前提与安装

- AstrBot `>=4.16,<5`；运行依赖要求 Python 3.10 或以上，本次离线测试使用 Python 3.12。Python 3.10 的异常组使用 requirements 中的 exceptiongroup backport；该解释器版本尚未单独验收。
- MCP SDK `>=1.30.0,<3`。客户端已分别测试 1.30.0 和 2.2.0；本次真实 AstrBot 4.28.1 宿主验证使用其声明允许的 MCP 1.30.0（宿主要求 `<2`）。其他 AstrBot/MCP 组合仍需验收，不应为了本插件绕过宿主的依赖约束。
- 账号、专用可撤销 API Key 和服务额度。工具输入会发送到百智云，调用可能消耗额度并产生费用。

本候选需要人工安装到隔离的 AstrBot 测试环境：将本目录复制为 `data/plugins/astrbot_plugin_baizhi_agent_toolkit/`，在该环境安装 `requirements.txt` 后加载。正式发布前不要把候选直接覆盖到生产机器人。

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

2026-09-20 另在官方 AstrBot 4.28.1 源码 [6914bc3](https://github.com/AstrBotDevs/AstrBot/commit/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334)、Python 3.12.14、MCP 1.30.0 的专用环境通过了 17 项隔离检查：真实 `Context` / `PluginManager` 加载本地插件副本，注册并导出三个 LLM 工具 schema，经 `FunctionToolManager` 的 `ToolSet` 权限 wrapper 调用；真实配置文件保存后重载、空列表关闭全部、恢复单个工具，以及卸载时移除工具、模块、命令注册和配置文件。配置与 SQLite 数据库位于隔离的 `ASTRBOT_ROOT`，临时文件使用独立 `TMPDIR`。

三工具执行使用真实宿主调用路径和 MCP SDK，只有远端 HTTP 响应由 `httpx.MockTransport` 模拟，Key 为合成值，所有外部 socket 连接被阻止。没有启动 WebUI、实际 LLM provider 会话或生产服务。修复前真实 wrapper 的四次调用均因 handler 多收一个位置参数失败；修复后 17 项全部通过。宿主验收在独立临时环境执行；普通单测不引入完整宿主依赖，也不等同于这组宿主验收。

## 发布前仍需完成

1. WebUI 安装与配置操作、真实命令分发、实际 LLM provider 会话及多插件冲突验收；其他受支持 AstrBot 版本还需兼容性验证。本次真实管理器加载本地副本不等于已验证 WebUI 安装或完整 Agent 回合。
2. 用已授权、专用可撤销 Key 核对现网 schema/权限，并完成有明确额度边界的三个工具生产验收。
3. OS 凭据保护、备份/导出与访问权限、网络/TLS/代理部署、并发和响应内存边界验收。本次保存重载仅证明配置值能持久化并生效，不证明加密或 OS 隔离。
4. 完整依赖锁/安全审查、发布身份及素材权利确认、候选人工复核；再单独发布并核验市场实际版本。

以上仍未完成的步骤不因隔离宿主检查通过而豁免。本次没有接受平台协议、发布正式版本或宣称市场审核通过。

MIT；见 LICENSE。
