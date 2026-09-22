# Runtime 插件接口 v1

Runtime 负责执行环境、插件清单、会话、任务队列、状态与产物；插件提供具体能力。
这是 ai-runtime 的 Python 插件接口，不是 Codex 的插件格式，也不是另一个 MCP Server。
MCP 工具保持通用，不为每个插件单独增加工具。

## 目录与注册

```text
ai-runtime/
  src/ai_runtime/       通用 Runtime
  plugins/effects/     效果测试插件：霓虹 + 风洞
  plugins/chrome-devtools/  CDP 调试插件
  plugins/adb/         Android 设备工具
  plugins/*/examples/  插件调用与组合排查脚本
  examples/            仅保留 Runtime 核心能力示例
  tests/               Runtime 与插件测试
```

在主仓库运行：

```powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins add D:/prj/ai-runtime/plugins/effects
.\.venv\Scripts\python.exe -m ai_runtime.plugins list
.\.venv\Scripts\python.exe -m ai_runtime.plugins get effects
```

默认清单在当前目录的 .runtime/plugins.sqlite3。服务使用自定义 data-dir 时，在子命令前加 `--data-dir 绝对路径`。
add 注册本机现有目录，不复制目录、不下载、不运行 pip、不导入代码。重复注册相同目录和清单为幂等；同 ID 不同路径或清单会被拒绝。源目录需保留。
清单变更在加载前检测，支持显式刷新同路径清单，不提供远程升级、删除注册或远程商店；unload 只卸载本会话实例。
插件是可信本机 Python 代码，权限等同 kernel，接口不是安全沙箱。

## 获取、新增、运行

MCP 提供：
- list_plugins：获取已注册插件。
- get_plugin(plugin_id)：查看元数据和动作参数说明。
- add_plugin(path)：注册本地目录。
- load_plugin(session_id, plugin_id, request_id)：排队创建插件实例。
- call_plugin(session_id, plugin_id, request_id, action, arguments, timeout=30)：排队执行动作，必要时自动加载。
- unload_plugin(session_id, plugin_id, request_id)：排队调用 close 并卸载实例。

后三者返回 execution ID，使用 wait_execution/get_execution 和 read_events 读取完成状态及 JSON 输出。
同一逻辑操作使用稳定 request_id；不确定结果时检查任务，不要换 ID 重放。调用失败的副作用不会自动回滚。
插件对象跟随 kernel，不跟随 MCP 连接；不同会话拥有独立实例。同一会话重复 load 返回同一实例。
卸载先阻止新增资源，调用插件 close，再兜底清理已登记资源；失败保留实例供重试清理，此时业务动作不可再调用。成功后旧 handle 不可调用。正常 Python 退出会尝试 close，强制终止 kernel 不能保证 Python 清理钩子完成。

SDK 用法（在 Runtime kernel 中）：

```python
runtime.plugins.list()
runtime.plugins.get("effects")
fx = runtime.plugins.load("effects")
fx.call("start", {"scene": "neon"})
fx.call("configure", {"scene": "neon", "changes": {"hue": 275}, "message": "改成紫色"})
fx.call("start", {"scene": "wind", "count": 800})
fx.call("configure", {"scene": "wind", "changes": {"wind": [100, -50]}})
fx.call("status")
fx.call("snapshot", {"scene": "wind"})
fx.call("stop", {"scene": "neon"})
runtime.plugins.unload("effects")
```

HTTP：GET /plugins、GET /plugins/{id}、POST /plugins {path}；
POST /sessions/{sid}/plugins/{id}/load、call、unload，使用与 MCP 对应的 JSON 字段（sid/id 在路径里）。全部使用原来的 Bearer 认证。

## 插件作者接口

每个插件目录必须有 plugin.json：

```json
{
  "id": "my-plugin",
  "name": "My plugin",
  "version": "0.1.0",
  "api_version": 1,
  "description": "What the plugin does",
  "entry": "plugin.py",
  "actions": {
    "ping": {
      "description": "Return context",
      "parameters": {}
    }
  }
}
```

id/action 使用小写字母开头，仅允许小写字母、数字、下划线及连字符，最多64字符。
entry 是目录内的相对 Python 文件，支持相对导入。parameters 是面向调用者的参数说明对象，v1 不声称执行完整 JSON Schema 校验；具体动作必须自行校验参数。

```python
class MyPlugin:
    def __init__(self, context):
        self.context = context

    def call(self, action: str, arguments: dict):
        if action != "ping" or arguments:
            raise ValueError("Expected ping with no arguments")
        return {"session_id": self.context.session_id}

    def close(self):
        pass

def create(context):
    return MyPlugin(context)
```

context 包含 api_version、session_id、session_dir、workdir、logger、preview、artifacts、resources。
create 只在首次加载时调用。创建过程中已登记的资源也会在失败时清理；未登记的资源仍由插件负责。
call 接收及返回 JSON 可序列化数据，拒绝 NaN/Infinity；文件和图片可用 runtime.save_artifact 保存后返回引用。
Runtime 校验清单声明的动作，业务参数由插件校验。先完成校验，再修改状态。
单会话的 MCP 调用由原任务队列串行处理；插件自己启动的后台线程需要自行同步。

生成新插件模板：

```powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins new D:/prj/ai-runtime/plugins/my-plugin --id my-plugin
.\.venv\Scripts\python.exe -m ai_runtime.plugins add D:/prj/ai-runtime/plugins/my-plugin
```

new 不覆盖现有目录，不自动注册。缺少依赖时由作者明确安装，Runtime 不自动安装插件依赖。

## 从旧版迁移

效果实现与调用示例统一放在 plugins/effects；旧迁移占位目录已删除。根目录 examples 仅演示 Runtime 核心能力。
既有 kernel 不会自动替换已导入的类。新建会话可使用新 SDK；更新守护进程后新增 HTTP 接口才会生效，重新连接 MCP 后才会加载新增6个工具。
为避免中断运行中的模拟，本次开发不会自动关闭旧服务。

## 插件业务日志

create(context) 收到的 context.logger 已绑定插件 ID。可使用 debug/info/warning/error/critical(msg, **fields)，或 log("INFO", msg, **fields)。msg 为字符串，额外信息通过命名字段传入，不使用 Python logging 的位置参数格式化。

```python
context.logger.info("连接设备成功", device="emulator-5554")
context.logger.warning("重试连接", attempt=2)
context.logger.error("读取失败", error_type="TimeoutError")
```

日志在发出时保存 UTC ISO 时间，携带 level、msg、plugin、fields；主服务补充 session_id 和 execution_id。通过 IOPub 回传后，以 plugin.log 事件持久化，可用 read_events 获取原始结构；主服务 LogRecord.runtime_log 也保留该字典。终端按实际级别和发出时间显示。DEBUG 默认不显示在 INFO 终端，但仍保存在事件记录中。

普通 print 和第三方 logging 不会自动改走此接口。后台线程可调用 logger，但任务归属取决于 IOPub 的 parent ID：后台消息可能没有 execution_id，或仍关联启动线程的任务；session_id 和插件名始终明确。

密码/token 等命名字段会脱敏，URL fragment 会隐藏；不要把凭证直接拼入 msg。此改动只统一业务日志接口和结构化传输，终端重定向仍为文本，未加入 JSONL 输出模式。

## 内置工具插件

- [Chrome DevTools](../plugins/chrome-devtools/README.md)：持久调试连接、求值、协议事件和截图。
- [ADB](../plugins/adb/README.md)：设备选择、shell、logcat、截图和端口转发。

各插件目录需分别 add 注册。新增注册不需要重启服务；list_plugins 可立即发现，call_plugin 首次调用自动加载。


## 更新本地插件清单

python -m ai_runtime.plugins refresh 插件ID 重新校验已注册目录中的 plugin.json，事务性更新其元数据。不允许改变 ID 或注册路径，不导入代码、不热替换运行实例。单纯 add 仍拒绝不同清单，避免无意覆盖注册。

现有会话需要明确 unload/load，新会话直接加载新版本。新增动作不需要重启主服务；如果 MCP 客户端仍显示旧工具说明，需要重新连接该 MCP 服务以刷新工具列表，不能靠重启 HTTP 服务刷新已运行的 STDIO Adapter。


## 统一上下文服务

插件由 Runtime 创建时，context 的服务已按插件与会话绑定：

~~~python
context.logger.info("任务完成", count=3)
view = context.preview.create(html=html, state={"score": 0}, name="score-view")
view.start()
view.publish({"score": 42})
artifact = context.artifacts.save(png_bytes, name="screen.png", mime="image/png")
~~~

preview.create 返回托管预览，提供 start、publish、update、snapshot、info、stop。自动登记其清理方法；stop 幂等，停止后重新展示应 create 新实例。插件不访问预览的内部字段。artifacts.save 直接使用 context.session_dir 保存，仍走现有 artifact.created 事件与 MCP 读取链路，不依赖进程环境变量决定产物所属会话。已有 runtime.save_artifact 等 SDK 接口保留兼容。

## 资源登记与清理

~~~python
connection = connect_to_service()
lease = context.resources.register("service-connection", connection.close, kind="connection")
# 不再需要连接时主动关闭，同时完成登记项：
lease.close()

stop = threading.Event()
worker = threading.Thread(target=lambda: run_loop(stop), daemon=True)
worker_lease = context.resources.track_thread(worker, stop=stop.set, timeout=3, name="worker")
worker.start()
~~~

在 kernel 的串行插件动作中完成登记和线程启动，再返回；不要与卸载并发启动线程。track_thread 不启动线程，需要提供协作停止方法。插件 close 可以调用 worker_lease.close()，或依靠 Runtime 兜底。线程 join 后仍存活会报告失败，不把超时当成成功，也不强行杀线程。

清理遵循：
1. 关闭服务入口，禁止登记新资源、创建预览和保存新产物。
2. 调用插件 close 做业务收尾。
3. 即使 close 失败，也按登记逆序尝试关闭全部剩余资源。
4. 保存每项结果和耗时；失败项保留，后续 unload 可重试。已经成功的资源不重复执行清理回调。
5. 只有 close 和资源清理全部成功，才移除实例和模块。

close 回调应可重试。普通回调没有可安全强制执行的硬超时，插件必须自行提供有界的网络关闭方法；track_thread 有明确 join 超时。只管理显式登记的资源，不能自动发现任意后台线程；kernel 被强杀也不能保证 Python 清理钩子执行。

create 中途失败同样会回收登记资源；如清理失败，保留仅可清理的句柄，先 unload 再 load。插件处于清理状态时禁止继续 call。

## 查看清理报告

为兼容现有代码，unload 仍返回布尔值；失败则抛异常。报告通过 SDK 和结构化事件提供：

~~~python
runtime.plugins.unload("effects")
report = runtime.plugins.cleanup_report("effects")
print(report["ok"], report["hook"])
print(report["results"])    # 最近 100 次资源清理结果
print(report["remaining"])  # 尚未成功关闭的全部登记项
~~~

SDK get(plugin_id) 同时提供 cleanup_pending 和当前 resources。这里的 resources.ok 表示登记资源已全部关闭，活动插件有资源时为 false，不是健康状态。

plugin.cleanup 事件包含报告，沿现有 IOPub、read_events、日志链路传输；通用事件脱敏会截断过长列表，完整 remaining 可通过 SDK 查询。错误保留类型，不直接记录可能包含凭证的异常正文。报告属于当前 kernel 内存，持久事件可跨连接读取。

主服务 HTTP/MCP get_plugin 仍只返回注册元数据；会话资源状态与 cleanup_report 通过 execute 在指定会话读取。

这些是 API v1 的新增上下文服务。已有 kernel 导入的旧 PluginManager 不会热替换；更新后用新建会话加载插件，无需关闭正在运行的旧预览。外部插件仅使用原有接口仍兼容。
