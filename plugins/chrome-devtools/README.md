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

## 0.3 JS 调试快捷操作

先 connect，再 prepare（或显式 Debugger.enable）以收集脚本事件。
已知目标等待启动时才使用 release_wait=true；不会自动解除断点暂停。

- scripts：query 按 URL 子串搜索（忽略大小写），offset/limit 分页。独立缓存最近最多 4000 个脚本，不受 200 条事件缓冲限制；cache_truncated 表示发生过淘汰。
- source：script_id、start_line（从 1 开始）、line_count（默认 80，上限 300）。输出最多 65536 字符，truncated 表示字符截断；源码请求本身仍受现有 CDP 消息大小限制。
- stack：返回最近观察到的暂停原因和最多 100 个帧，不主动暂停。运行中或未观察到暂停时 pause=null，不能把 unknown 当成 running。
- breakpoint_set：url 为脚本精确 URL，line、column 从 1 开始，支持 condition。返回 CDP 原始 breakpointId/locations（原始 locations 使用零基行列），locations 为空可能表示尚未绑定。
- breakpoint_remove：传 breakpoint_id 显式删除。
- resume：明确恢复 JS 执行。断点命中后不会自动恢复。

以上操作可传 session_id（CDP 子会话 ID，不是 ai-runtime 会话 ID）；需要发协议命令的操作可设 timeout。
脚本索引按 CDP 会话区分，执行上下文全部清空、连接断开或重连会清理相关缓存。
设置断点、条件表达式和 resume 会影响调试目标，按需显式调用。低层 command/evaluate/events 保留。

示例（使用 MCP 的 call_plugin_and_wait，plugin_id="chrome-devtools"）：
action="scripts", arguments={"query":"assets/main"}
action="source", arguments={"script_id":"23","start_line":1,"line_count":60}
action="stack", arguments={}

更新已注册插件：python -m ai_runtime.plugins refresh chrome-devtools。
已有会话需 unload/load 才会载入新版；这会关闭旧调试连接。也可在主服务重启后新建会话加载。

## 0.4 完整 JS 调试流程

1. scripts 查找文件，再 source_search 搜索源码内容。query 为字面量，不按正则执行；可用 script_id 限定文件或 url_query 筛选已加载文件。offset/max_scripts 按脚本分页，limit 限制命中数量。超时或错误会在 errors 中报告；truncated 时需缩小范围，不能认为已经返回全部命中。
2. source 读取指定行附近的源码，breakpoint_set 设置断点。
3. 命中后 stack 返回函数、文件、行列、call_frame_id、this 和 scopes。栈中的行列从 1 开始；目标未提供 URL 时用脚本索引补全。
4. variables(object_id=scope.object.objectId) 查看局部变量、闭包变量或对象自己的属性。通过 value.objectId 继续展开嵌套对象，offset/limit 分页；getter 仅展示描述符，不主动执行。长字符串显示前 2000 字符。
5. frame_evaluate(call_frame_id=..., expression=...) 在选中帧求值，默认 throwOnSideEffect=true。若目标不支持或表达式可能有副作用，会报错，不自动退回不受限求值；确需修改时显式传 allow_side_effects=true。异常见 exceptionDetails，返回对象可用 variables 展开。
6. step_over / step_into / step_out 实现单步；pause 主动请求暂停，resume 继续运行。步骤命令的响应不等于已经收到新的暂停事件，随后读取 stack 确认；最后 breakpoint_remove 清理测试断点。

object_id / call_frame_id 属于当前目标和暂停现场，恢复、导航或重连后应重新获取，不持久复用。
搜索和行断点针对目标已加载的生成 JavaScript，暂未实现 source map 映射回 TypeScript。
搜索和属性读取底层仍可能返回完整 CDP 响应，受现有 8 MiB 消息上限约束，分页限制的是返回给调用方的结果。

在 Windows 原生 Cocos 游戏实测：assets/main/index.js 第 664 行命中 FriendsUI._buildUI；
调用栈包含 _buildUI → start → 引擎调度，局部变量 root=Node、lt=UITransform、backBtn=undefined；
帧求值 typeof root 返回 object；step_over 后停在 665 行；删除断点并 resume 成功。

## 0.5 事件等待与暂停摘要

wait_for_pause 使用接收线程的条件通知等待 Debugger.paused，不反复发送 stack 查询。
默认 wait_seconds=5（0–20），summary=true；已暂停则立即返回，after_pause 可指定上次的 pause_id，仅等待更新现场。
返回 status 为 paused、timeout、disconnected 或 state_changed。等待超时不取消目标执行、不清理断点、不自动恢复。
每次连接和 CDP 子会话的现场分开处理；连接断开会立即唤醒等待者。pause_id 在插件实例内单调递增，重新加载实例后须重置游标。

pause_snapshot 直接读取当前暂停现场。两种动作的摘要默认前 5 层调用栈、栈顶局部作用域的 12 个变量和附近 7 行源码。
frame_limit=1–20、variable_limit=0–50、source_lines=0–31；0 可关闭变量或源码读取。
字符串最多 200 字符，源码每行最多 300 字符，明确标记截断；复杂对象仅保留 objectId 供 variables 按需展开，不调用 getter，不求值。
timeout 默认 3 秒，是摘要查询共享的总预算；超时或部分失败保留其他信息，并返回 partial/errors。
检查期间若现场已恢复、重新暂停或断开，返回 state_changed，避免将旧变量当作当前现场。

通过 call_plugin_and_wait 调用 action="wait_for_pause" 即可等待并拿到摘要。
注意 MCP wait_seconds（等待 Python 任务）与 arguments.wait_seconds（等待断点）是两层预算；
外层 timeout 应大于断点等待加摘要预算。例如 timeout=30，arguments={"wait_seconds":15,"timeout":3}。
若快捷调用先返回 done=false，使用 read_execution_result 续读，不重新下发等待任务。
一次会话按顺序执行任务：目标操作应先触发，或由游戏自身运行/用户操作触发，再等待断点；不要在同一 Kernel 中把触发任务排到正在等待的任务后面。
step_over 后传上次 pause_id 等待，防止读到单步前的缓存。
