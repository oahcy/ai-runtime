# 通用 Live Preview

主项目负责本机 HTTP / WebSocket 服务、凭证校验、JSON 状态、版本和最近30条更新记录。
页面、业务默认值及业务校验由使用方提供。运行中的 kernel 与 MCP 客户端连接独立，关闭 kernel 才结束其服务。

## 基础用法

```python
from ai_runtime.preview import Preview
view = Preview(state={"score": 0})
view.start()  # 默认页面显示实时 JSON 状态
view.update(score=42, message="更新分数")
view.stop()
```

无参的 runtime.preview.start()/update() 现在对应通用 JSON 查看器，不再隐式启动霓虹动画。

## 自定义页面

```python
from pathlib import Path
view = Preview(
    html=Path("index.html").read_text(encoding="utf-8"),
    state={"score": 0},
)
```

html 为可信本机代码，在实例创建时读取，不自动热替换。只提供根页面，不托管任意文件目录。
浏览器连接同源 /ws，第一条消息发送 {"token": location.hash.slice(1)}。
服务端返回 {revision, state, history}；history 中每条含 revision、message、changes、time。
收到消息后使用 state 更新自己的界面。update 按顶层键合并 JSON，嵌套对象整体替换；拒绝 NaN/Infinity 等非 JSON 值。
状态与更新参数复制后保存，调用方修改嵌套对象不会隐式改变已发布数据。

## 示例

- [霓虹星轨](../examples/neon/README.md)：浏览器动画，Python 实时改变参数。
- [粒子风洞](../examples/live-lab/README.md)：Python 持续模拟，浏览器显示粒子位置。

只监听 127.0.0.1，默认随机空闲端口。凭证独立于 Runtime 主 token，通过 URL fragment 提供。
WebSocket 校验 Origin 和凭证，不提供浏览器执行 Python 的接口。
重连立即获得完整最新快照；50ms 轮询检测状态变化，快速更新可能合并。
info() 获取链接，snapshot() 读取状态，stop() 停止预览。状态不自动持久化。
从旧版迁移霓虹示例时，请在新 kernel 使用 NeonPreview；已有 kernel 中载入的旧类不会自动热更新。
