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
  examples/            使用插件的简短脚本
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
清单变更在加载前检测，版本 1 不提供升级、删除注册或远程商店；unload 只卸载本会话实例。
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
卸载先 close，失败则保留实例供重试；成功后旧 handle 不可调用。插件应负责停止线程、释放连接和预览窗口。正常 Python 退出会尝试 close，强制终止 kernel 不能保证 Python 清理钩子完成。

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

context 包含 api_version、session_id、session_dir、workdir、logger。
create 只在首次加载时调用，失败时插件应清理其已创建资源。
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

原 examples/neon 与 examples/live-lab 的实现已移动到 plugins/effects，原示例位置保留插件调用说明。
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
