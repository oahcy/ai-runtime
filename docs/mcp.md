# 本地 MCP 接入

## 当前实现

```text
支持本地 STDIO 的 AI 客户端
    → ai_runtime.mcp_server（客户端启动的子进程）
    → 本机 HTTP + Bearer token
    → ai-runtime 服务
    → 持久 IPython kernel
```

Adapter 只做协议转换，不直接访问 Runtime 数据库，也不拥有 kernel。MCP 断开或重启不会关闭已有会话。客户端通过 session_id 继续工作。

本次采用官方 MCP Python SDK 的受维护 v1 系列，依赖范围 `mcp>=1.28,<2`，锁定测试版本见 requirements.lock。MCP 是可选依赖，核心 HTTP 服务无需安装它。

## 1. 启动 Runtime

```powershell
cd D:\prj\ai-runtime
.\.venv\Scripts\python.exe -m pip install -e ".[test,mcp]"
.\.venv\Scripts\python.exe -m ai_runtime
```

本次开发环境已经安装依赖。若 Runtime 已经运行，可以继续使用，不需要为了 Adapter 重启 kernel。

## 2. 配置本地 MCP 客户端

在支持 STDIO 的客户端中添加服务：

- 名称：`ai-runtime`
- 程序：`D:/prj/ai-runtime/.venv/Scripts/python.exe`
- 参数：`-m ai_runtime.mcp_server --runtime-url http://127.0.0.1:8765 --token-file D:/prj/ai-runtime/.runtime/token`

完整 JSON 示例：[configs/mcp.stdio.example.json](../configs/mcp.stdio.example.json)。客户端配置文件的外层结构可能不同，请按客户端格式使用。

Codex 配置片段：[configs/codex.mcp.example.toml](../configs/codex.mcp.example.toml)。也可在支持的 Codex CLI 环境执行：

```powershell
codex mcp add ai-runtime -- D:/prj/ai-runtime/.venv/Scripts/python.exe -m ai_runtime.mcp_server --token-file D:/prj/ai-runtime/.runtime/token
```

这里提供配置示例，不会自动修改用户的全局客户端配置。若 Runtime 使用自定义 --data-dir 或端口，需同步调整参数。

不要将 token 内容写入配置示例或模型提示词；Adapter 每次请求从指定文件读取 token。SDK 的 stdout 专用于 MCP 协议，日志写 stderr；手动运行 Adapter 后等待输入是正常现象。

官方文档：[本地 STDIO MCP 配置](https://developers.openai.com/codex/mcp)。

## 3. 验收对话

接入后，在新对话中要求模型：

> 使用 ai-runtime 创建一个会话，工作目录 D:/prj/ai-runtime。先执行 answer = 40，再提交另一段代码 answer += 2; print(answer)。检查执行结果和事件，确认输出 42，并记住 session_id。

典型调用顺序：

1. `create_session(cwd)` 返回 session_id。
2. `execute(session_id, request_id="step-1", code="answer = 40")` 返回执行 ID。
3. `wait_execution(execution_id)` 等待执行结束。
4. 第二次 execute，使用新的 request_id 和相同 session_id。
5. `read_events(session_id, after=0)` 获取 stdout 和产物 ID，保存 cursor。
6. 下次 read_events 传上次 cursor，避免重复读取。
7. 如输出 artifact.created，调用 `read_artifact(artifact_id)`；图片直接作为 MCP ImageContent 返回。

同一逻辑提交的重试必须使用同一个 request_id、代码和 timeout。未知状态不能直接换 ID 重试，否则可能重复产生外部副作用。

## 暴露的工具

| 工具 | 行为 |
|---|---|
| list_sessions / get_session | 查询会话和 generation |
| create_session | 创建独立持久 kernel |
| execute | 排队执行 Python，立即返回执行记录 |
| get_execution / wait_execution | 查询 / 最多等待 20 秒；停止等待不会取消执行 |
| read_events | 按 cursor 读取历史、stdout、错误和产物事件 |
| interrupt | 请求中断或取消排队任务，须继续确认最终状态 |
| close_session | 关闭 kernel 并丢弃内存 |
| restart_session | 显式重建 kernel；禁止自动故障重试调用 |
| read_checkpoint | 读取 JSON 检查点 |
| read_artifact | 返回图片、文本或嵌入文件，限制 8 MiB |

工具读写注解仅供客户端决策，不构成权限隔离。执行与会话修改均标记为可能产生副作用。

## 与 ChatGPT 网页端的区别

本版只实现本地 STDIO，不提供远程 MCP HTTP 服务。ChatGPT 网页开发者模式使用远程 MCP（SSE/streaming HTTP），不能把这里的 Python 命令直接填成远程服务 URL。后续远程接入应单独设计认证与连接边界，不直接把可执行任意 Python 的本机 HTTP API 暴露出去。

官方说明：[ChatGPT developer mode](https://platform.openai.com/docs/guides/developer-mode)。本地桌面客户端是否有 MCP 设置，应按实际客户端版本和官方说明确认。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mcp.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

MCP 集成测试会启动独立 HTTP 服务和真实 STDIO Adapter，通过官方 MCP ClientSession 完成 initialize、tools/list、tools/call；退出第一个 Adapter 后启动第二个，确认变量保留和图片返回。测试不读取用户运行服务的 token，也不操作用户已有 kernel。

## 当前边界

- Adapter 仅接受 loopback HTTP origin，禁用环境代理、HTTP 重定向和自动写请求重试。
- 本地插件接口已实现：新增 list_plugins、get_plugin、add_plugin、load_plugin、call_plugin、unload_plugin 六个工具；详见 [插件说明](plugins.md)。已提供 [DevTools](../plugins/chrome-devtools/README.md) 与 [ADB](../plugins/adb/README.md) 插件，通过相同的 call_plugin 调用。
- token 代表本机可信执行权限；首版依旧不是沙箱。
- WebSocket 仍是 Runtime API 能力；MCP 层目前按游标读取事件，没有向闲置模型主动推送唤醒事件。

插件接口加入后共有18个 MCP 工具；新增工具需在重启服务并重新连接 MCP 后使用。
