# Chrome DevTools 调试插件

通过持久 WebSocket 连接调用 Chrome DevTools Protocol（CDP），支持浏览器页面和提供 V8 inspector 的运行环境。插件 ID：`chrome-devtools`。

## 注册与使用

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins add D:/prj/ai-runtime/plugins/chrome-devtools
```

下面的 Python 在 Runtime 会话中执行：

```python
dev = runtime.plugins.load("chrome-devtools")
targets = dev.call("targets", {"endpoint": "http://127.0.0.1:9222"})
print(targets)  # 按 URL/title 选择目标，不要盲目连接列表第一项
dev.call("connect", {"url": "ws://127.0.0.1:9222/devtools/page/目标ID"})
dev.call("command", {"method": "Runtime.enable"})
dev.call("evaluate", {"expression": "globalThis.runtimeProbe = 40"})
dev.call("evaluate", {"expression": "globalThis.runtimeProbe += 2"})
events = dev.call("events", {"after": 0, "limit": 50})
print(events)
dev.call("events", {"after": events["cursor"]})
dev.call("screenshot")  # 仅支持 Page.captureScreenshot 的页面目标
runtime.plugins.unload("chrome-devtools")
```

connect 也接受完整的 `devtools://devtools/bundled/inspector.html?...&ws=127.0.0.1:端口/目标ID` 链接。仅连接本机回环地址；插件不启动 Chrome，不自动选择目标或启用调试端口。targets 需要目标提供 HTTP /json/list；只提供 WebSocket 的运行环境直接 connect。

## 动作

| 动作 | 参数与行为 |
|---|---|
| targets | endpoint（默认 http://127.0.0.1:9222）、timeout；列出调试目标 |
| connect | url、timeout；先断开旧连接，再连接指定目标 |
| status / disconnect | 无参数；查询 / 关闭调试连接 |
| command | method、params（对象）、timeout、session_id；调用任意 CDP 命令 |
| evaluate | expression、await_promise（默认 true）、return_by_value（默认 true）、context_id、timeout、session_id |
| events | after（默认 0）、limit（1–100，默认 50）；读取接收线程缓冲的协议事件 |
| screenshot | timeout、session_id；返回 PNG 产物引用 |

timeout 单位为秒，默认 10，范围 0.1–120。session_id 是 CDP 的扁平会话 ID，**不是** Runtime 的会话 ID。连接到浏览器级目标时，可以通过 Target.attachToTarget（flatten=true）取得它。

evaluate 原样返回 CDP 结果；JavaScript 异常见 exceptionDetails。V8-only 目标通常没有 Page/DOM/Network 域，截图请配合 adb 插件。协议错误会使调用失败；命令超时不表示目标执行已取消，不会自动重试。

协议事件需要先启用相应域（例如 Runtime.enable、Network.enable）。缓冲保留最近 200 条；单条超过 32 KiB 会标记 truncated 并省略内容，游标过旧返回 dropped=true。该缓冲属于插件内存，不是 Runtime 的持久事件库；重新加载会重置。持续采集需要在会话中自行安排轮询。

截图通过 runtime.save_artifact 保存，可在 read_events 中找到 artifact.created，再用 MCP read_artifact 读取。业务日志使用 context.logger。

MCP 无需新增专属工具：call_plugin 的 plugin_id 为 chrome-devtools，action/arguments 对应上表。先 wait_execution，再 read_events 获取结果。外层执行 timeout 应大于插件内部 timeout（例如 60 秒与 10 秒）。

协议参考：[Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)。


## 0.2 连接诊断与启动探测

connect 默认关闭 WebSocket ping 心跳，兼容本次遇到的原生 inspector。可显式传 heartbeat=true 启用（需要当前 websockets 支持）。关闭心跳不保证识别半开连接，仍通过命令超时诊断。连接不会自动重连或重放指令。

status 将三个概念分开：
- transport：connected / disconnected，只描述 WebSocket。
- responsiveness：unknown / responsive / unresponsive，依据最近响应或超时，不能保证下一条指令成功。
- execution：按 CDP session ID 记录收到的 paused / running；未收到事件时没有对应条目，不能凭超时断言应用正在等待调试器。

同时返回收发时间、最近事件、关闭码与原因、最近 50 条命令元数据及迟到响应计数。不会在诊断历史保存表达式、参数或响应正文。迟到响应会更新对应记录，绝不作为后续指令的结果。超过历史容量的旧响应不再关联统计。重连清空这些连接诊断，保留上次断开原因，清空旧目标事件，避免混入上一次调试的脚本；事件序号不回退，connect 返回本连接起始 cursor。

~~~python
dev.call("connect", {"url": debugger_url})
report = dev.call("prepare", {"timeout": 6})
print(report)
# 已知应用正等待调试器启动，并希望它继续启动时，显式使用：
report = dev.call("prepare", {"release_wait": True, "timeout": 6})
~~~

prepare 使用一个总时间预算，按顺序执行可选 runIfWaitingForDebugger、Runtime.enable、Debugger.enable、简单求值。第一处失败就停止，返回步骤结果和诊断，不连发多条超时指令。收到 paused 时停止求值，不自动 resume。release_wait=true 会改变应用执行状态，不能仅因为超时就猜测需要它；返回 ready 只确认简单求值成功，不代表页面启动成功。

这些操作启用协议域，并不设置断点、修改业务变量或自动重启应用。业务启动错误结合 [ADB diagnose](../adb/README.md) 检查。

旧安装更新清单：
~~~powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins refresh chrome-devtools
~~~
已加载旧版本的会话先 unload 再 load；此操作关闭该会话的调试连接。


## 组合排查示例

[examples/diagnose_app.py](examples/diagnose_app.py) 将 ADB 启动日志与 DevTools 探测组合起来，需同时注册 adb 插件。在项目根目录对应的 Runtime kernel 中：

~~~python
import runpy
diagnose_app = runpy.run_path("plugins/chrome-devtools/examples/diagnose_app.py")["diagnose_app"]
report = diagnose_app(runtime, "你的应用包名", "ws://127.0.0.1:5086/目标ID")
~~~

已知应用等待调试器、且希望它继续启动时，传 release_wait=True。
