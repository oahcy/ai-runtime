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
