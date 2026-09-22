# ADB 工具插件

插件 ID：`adb`。调用本机 Android platform-tools，通过结构化动作控制设备并保存截图。优先从 PATH 查找 adb，也检查 ANDROID_SDK_ROOT、ANDROID_HOME 和 Windows 默认 Android SDK 位置。

## 注册与使用

```powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins add D:/prj/ai-runtime/plugins/adb
```

下面的 Python 在 Runtime 会话中执行：

```python
adb = runtime.plugins.load("adb")
print(adb.call("devices"))
adb.call("configure", {"serial": "你的设备序列号"})
print(adb.call("shell", {"command": "getprop ro.product.model"}))
adb.call("screenshot")
print(adb.call("logcat", {"lines": 100}))
```

若没有自动找到程序，configure 传入 adb_path（可执行文件绝对路径）。设备需要已开启调试并授权；插件不会代替用户批准设备授权。没有配置 serial 时，仅在恰好一台就绪设备时自动选择；多个设备时要求明确指定。每次动作可传 serial 覆盖默认值。

## 动作

| 动作 | 参数与行为 |
|---|---|
| configure | adb_path、serial；配置程序与默认设备，serial=null 清空默认设备 |
| status | 返回程序路径和默认设备 |
| devices | 返回设备列表，包含 offline、unauthorized 状态 |
| shell | command；在设备 shell 执行字符串 |
| screenshot | 使用 exec-out screencap -p，返回 PNG 产物引用 |
| logcat | lines（1–2000，默认 200）；读取最近日志快照，不持续阻塞 |
| start_app | component，例如 com.example.app/.MainActivity |
| forward | local（如 tcp:9223 或 tcp:0）、remote（如 tcp:9222 或 localabstract:chrome_devtools_remote） |
| run | args 字符串数组；执行其他设备命令，例如 ["shell", "getprop", "ro.product.model"] |

设备动作支持 serial、timeout；timeout 单位为秒，默认 15，范围 0.1–120。设备发现另有独立的 15 秒超时，MCP 外层执行建议使用 timeout=60。

普通命令返回 returncode、stdout、stderr、truncated；**必须检查 returncode**，非零时 Runtime 执行本身仍可能成功。文本 stdout 最多返回 1 MiB，stderr 最多 64 KiB；截图最多 16 MiB（MCP read_artifact 目前最多 8 MiB）。

本机进程使用参数数组和 shell=False；shell 动作的 command 仍由 Android 的 shell 解释。run 是通用命令接口，设备修改权限与本机 adb 相同。超时停止本机客户端，不保证设备端动作回滚，不自动重试。

端口转发保留在共享 ADB server 中，卸载插件不会移除。完成后主动调用 run，例如 {"args": ["forward", "--remove", "tcp:9223"]}。插件不会在卸载时 kill-server 或断开其他设备。

截图通过 runtime.save_artifact 保存，read_events 返回 artifact.created，MCP read_artifact 可读取图片。插件业务日志统一走 context.logger，包含插件名和设备序列号。

## 与 DevTools 配合

在明确设备提供的调试 socket 后，先 forward 到本机端口，再用 chrome-devtools 的 targets/connect 连接。例如设备 Chrome 的调试 socket 可转发至 tcp:9223；原生 V8 的 socket/端口由应用决定。截图使用 adb，运行时查询使用 DevTools，两者各自持久保留配置或连接。

MCP 仍使用通用 call_plugin（plugin_id="adb"），随后 wait_execution 和 read_events；不需要添加新的 MCP Server。

参考：[Android Debug Bridge](https://developer.android.com/tools/adb)。


## 0.2 启动诊断

~~~python
report = adb.call("diagnose", {
    "package": "global.sud.op.test.hello",
    "timeout": 12
})
print(report)
~~~

只读采集设备选择、亮屏状态、前台窗口、包名对应 PID，以及最近 2000 行该进程日志中的错误和加载记录。输出最多 120 行摘要；某步骤失败会保留其余结果和 errors，使用总时间预算避免多次长超时。

摘要过滤常见凭证字段、HTTP 客户端日志、解密响应行，去掉 URL 查询/片段。它是尽力脱敏，不是任意日志的秘密检测器；原始 run/logcat 不做此过滤。进程不存在或历史日志被覆盖时，报告不会猜测原因，也不会自动唤醒、启动或重启应用。多进程应用只覆盖 pidof 精确包名返回的进程，可能不包含带冒号的子进程。

发现资源下载 Not Found 会给出部署路径检查提示，不断言是唯一根因。组合排查见 [diagnose_app.py](../chrome-devtools/examples/diagnose_app.py)：先收集 ADB 证据，再限时探测 CDP，无响应时仍返回设备报告。

旧安装执行 python -m ai_runtime.plugins refresh adb，已有会话 unload/load 后使用新动作。
