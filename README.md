# Local AI Runtime

供 AI 连续调用的本地 Python 执行环境。一个逻辑会话对应一个常驻 IPython kernel；客户端断开不影响会话。

当前为可运行的 v0.1 原型。仅支持可信本机模式，**不是沙箱**。执行代码拥有启动用户的文件、网络与进程权限。

## 已实现

- 创建、列出、关闭、显式重建会话；重建时 generation 增加。
- 同一会话串行执行，不同会话独立 kernel；每服务最多 8 个存活会话，单会话最多 100 条排队执行。
- 通过 request_id 去重提交；同一个 ID 改变代码或超时参数会返回冲突。
- stdout、stderr、表达式结果、异常和富媒体输出；后台输出由独立接收循环持续收集。
- HTTP 查询与 WebSocket 事件流；按 seq 游标重连、补读，SQLite 保存执行历史。
- 超时中断、手动中断、取消排队执行；没有停止确认则标为 unknown，并阻止该会话继续执行。
- PNG/JPEG 自动保存为产物；SDK 保存任意文件和显式 JSON 检查点。
- 本机回环监听、Bearer token、数据目录单实例锁。

完整框架设计见 [docs/architecture.md](docs/architecture.md)。

## 安装与运行

需要 Python 3.11+。在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m ai_runtime --data-dir .runtime --port 8765
```

服务前台运行，Ctrl+C 关闭服务及其 kernel。API 文档：<http://127.0.0.1:8765/docs>。

首次启动将生成 `.runtime/token`。在 Swagger 的 Authorize 中输入该文件的内容；Python 客户端自动添加 Bearer 前缀。不要把 token 放进 URL。

本项目已建立 `.venv` 并安装依赖，可直接使用第三条命令。`requirements.lock` 记录本次 Windows/Python 3.12 验证环境的精确依赖版本，可用于重现；它不是跨平台锁文件。

## 跨请求保留变量

在另一个终端执行：

```powershell
.\.venv\Scripts\python.exe examples/persistent_session.py
```

示例创建会话，第一次设置 `answer = 40`，换一个客户端后执行 `answer += 2`，得到 42，保存检查点和文本产物。示例保留会话，供后续继续执行。

也可以直接使用客户端：

```python
from pathlib import Path
from ai_runtime.client import Client

client = Client(Path('.runtime/token').read_text().strip())
session = client.create_session(Path.cwd())
job = client.execute(session['id'], 'value = 40', request_id='step-1')
print(client.wait(job['id']))
job = client.execute(session['id'], 'print(value + 2)', request_id='step-2')
print(client.wait(job['id']))
print(client.events(session['id']))
```

`client.wait()` 超时只代表客户端停止等待，不会取消服务端执行。

## Kernel 中的 SDK

每个 kernel 自动执行 `from ai_runtime import sdk as runtime`：

```python
runtime.checkpoint_save({'phase': 'inspecting', 'target': 'game-1'})
state = runtime.checkpoint_load()
runtime.save_artifact('hello', name='report.txt', mime='text/plain')

from IPython.display import display, Image
display(Image(filename='screenshot.png'))
```

检查点仅接受 JSON 可表示的数据。浏览器连接、Socket 和 Python 对象不属于检查点。

## 接口

除 `/health` 和 API 文档外，所有 HTTP 接口都要求 `Authorization: Bearer <token>`。WebSocket 握手使用同一请求头，不使用查询参数携带 token。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST / GET | `/sessions` | 创建 / 列出会话 |
| GET / DELETE | `/sessions/{id}` | 查询 / 关闭会话 |
| POST | `/sessions/{id}/restart` | 明确丢弃内存并重建 kernel |
| POST | `/sessions/{id}/executions` | 提交 `{request_id, code, timeout}`，timeout 单位为秒 |
| GET | `/executions/{id}` | 读取执行状态 |
| POST | `/executions/{id}/interrupt` | 中断运行 / 取消排队 |
| GET | `/sessions/{id}/events?after=0` | 分页读取事件 |
| WS | `/sessions/{id}/events/ws?after=0` | 回放后继续读取事件 |
| GET | `/artifacts/{id}` | 下载产物 |
| GET | `/sessions/{id}/checkpoint` | 读取显式检查点 |

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试会启动真实 IPython 子进程，覆盖变量保留、独立会话、失败后继续、输入禁用、超时/中断、未知状态、检查点、产物、鉴权和 WebSocket 回放。

## 已知边界

- 客户端重连保留变量；服务退出或 kernel 重建不保留内存。服务重启后旧会话需显式 restart，不会重放历史代码。
- 正常关闭会停止 kernel。用户代码自行启动的后台进程不提供完整进程树回收保证；Windows Job Object 等资源约束留待后续实现。
- Windows 的部分阻塞调用无法及时响应中断；5 秒内未确认停止则将会话置为 lost、执行置为 unknown，等待显式重建。
- 单次执行持久化输出预算为 4 MiB，超出后记录 output.truncated；该限制不是网络、内存或磁盘的系统级配额。
- 暂无自动清理历史、环境依赖快照、桌面适配器、模型调度器、审批系统或远程多用户部署能力。
- 富媒体产物采用下载方式返回；不在服务中直接执行 HTML/JavaScript 输出。

## MCP 接入 AI 客户端

本地 STDIO MCP Adapter 已实现，可复用现有 HTTP 服务与持久会话。参见 [MCP 接入说明](docs/mcp.md) 和 [配置示例](configs/mcp.stdio.example.json)。

安装可选依赖：`python -m pip install -e ".[test,mcp]"`。MCP 连接由客户端启动 Adapter；不会自动修改用户客户端配置。

## 实时预览

通过 `runtime.preview.start()` 打开持续动画窗口，再用 `runtime.preview.update(...)` 实时修改画面。详见 [Live Preview](docs/live-preview.md)。
