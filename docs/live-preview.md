# Live Preview：实时观看 Python 修改

无需重启已有 Runtime 服务。新建 kernel 会自动提供 `runtime.preview`；旧 kernel 可执行 `from ai_runtime import preview`。

在 MCP 的同一个会话里运行：

```python
from ai_runtime import preview
preview.start()
```

返回 `url` 是浏览器预览地址，保留完整的 `#...` 片段再打开。页面持续播放动画，无需重复生成 GIF，也无需刷新。接着分别执行：

```python
preview.update(hue=280, message="将星轨改为紫色")
preview.update(speed=1.2, particles=1000, message="加速旋转并增加粒子")
preview.update(rings=7, glow=1.1, spread=1.1, message="扩展光环并增强光晕")
```

参数：
- `hue`：0–360，色相。
- `speed`：0–3，动画速度，0 表示停止时间推进。
- `particles`：0–2000，整数。
- `rings`：1–10，整数。
- `glow`：0–1.5。
- `spread`：0.5–1.5。
- `title`：最多 80 字符。
- `message`：最多 300 字符，展示在操作记录里。

`preview.snapshot()` 读取状态和最近 30 条修改记录；`preview.info()` 读取当前链接；`preview.stop()` 停止服务。参数整批校验，失败不修改任何状态。

## 生命周期与通信

`MCP execute → kernel 中的 preview.update → WebSocket → 浏览器 Canvas`。

每个 kernel 拥有一个默认预览实例，后台线程运行本地服务，只监听 127.0.0.1，默认由操作系统选择空闲端口。MCP 断开不会停止它，关闭或重启 kernel 则会停止。再次 start 会启动新服务；这是内存状态，不会写入 checkpoint。

浏览器每次连接都获得最新完整状态（含最多 30 条记录），后续更新约 50ms 内检测并推送。快速连续更新可能合并成最新画面。动画由浏览器 requestAnimationFrame 驱动，帧率取决于设备；参数更新不会重置动画时钟。关闭预览标签页不会停止 Python kernel。

暂停按钮只暂停本地画面时间，仍接收 Python 更新；鼠标互动也只影响本地视角。

预览使用独立随机凭证，通过 URL fragment 提供、WebSocket 第一条消息验证；不使用 Runtime 主 token，不进入 HTTP URL 或访问日志。连接还校验同源 Origin。页面不提供执行 Python、修改文件或提交场景状态的接口。完整链接仍应只分享给可信的本机用户。

这是第一个内置预览模块，当前渲染器是参数化霓虹粒子场景。尚未实现任意 Python 图形窗口共享、任意 HTML 热替换或通用第三方插件发现机制。
