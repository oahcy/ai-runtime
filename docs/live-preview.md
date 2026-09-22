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

html 为可信本机代码，在实例创建时读取，不自动热替换。Runtime 自动在页面脚本前注入客户端接口，只提供根页面，不托管任意文件目录。

## 插件只发布数据

~~~python
from ai_runtime.preview import Preview
view = Preview(html=html, state={"score": 0})
view.start()
view.publish({"score": 42})  # 原子替换完整状态，返回 revision；适合逐帧更新
view.update(score=43, message="得分变化")  # 顶层合并，并记录操作历史
view.stop()
~~~

publish 不记录逐帧历史，避免大帧数据累积；update 保留最近 30 条操作记录。两者验证 JSON、拒绝 NaN/Infinity、复制嵌套值，调用者后续修改原数据不会改变已发布内容。插件无需继承 Preview 或访问内部锁、状态与版本字段。

## 页面只订阅状态

~~~html
<script>
const unsubscribe = RuntimePreview.subscribe((state, meta) => {
  render(state); // 插件自己的绘制函数
  // meta.revision、meta.history 可用于显示版本和操作记录
});
const unsubscribeStatus = RuntimePreview.onStatus(status => {
  // idle / connecting / connected / disconnected / unauthorized
  document.getElementById("status").textContent = status;
});
// 不再需要预览时分别调用 unsubscribe()、unsubscribeStatus()
</script>
~~~

接口在插件脚本运行前可用；首次订阅自动连接，多个订阅共享连接，最后一个订阅取消后停止连接。订阅后立即回放缓存的最近状态（如有）；回调失败不会阻止其他订阅者接收。回调中的数据请视为只读。

WebSocket 地址、凭证读取、鉴权、消息解析、断线重连由 Runtime 管理，插件页面不写这些逻辑。无效凭证停止重试，换用有效链接可重新连接。连接恢复后接收完整最新状态，不回放每一帧；高频更新可能合并。该接口只读，页面交互若只影响本地显示无需回传；尚未提供浏览器调用 Python 的接口。

## 传输实现（Runtime 内部）

当前使用同源 WebSocket，校验 Origin 和链接凭证。传输仍然能在浏览器 Network 中看到；“插件不可见”指插件不依赖协议实现，并不是隐藏网络流量。以后可替换传输实现而保持 publish / subscribe 不变。

## 示例

- [effects / neon](../plugins/effects/README.md)：浏览器动画，Python 实时改变参数。
- [effects / wind](../plugins/effects/README.md)：Python 持续模拟，浏览器显示粒子位置。

只监听 127.0.0.1，默认随机空闲端口。凭证独立于 Runtime 主 token，通过 URL fragment 提供。
WebSocket 校验 Origin 和凭证，不提供浏览器执行 Python 的接口。
重连立即获得完整最新快照；50ms 轮询检测状态变化，快速更新可能合并。
info() 获取链接，snapshot() 读取状态，stop() 停止预览。状态不自动持久化。
从旧版迁移时，注册 effects 插件后在新 kernel 使用 runtime.plugins.load("effects")；已有 kernel 中载入的旧类不会自动热更新。


## 插件使用托管预览

在插件中优先使用 context.preview.create(html=html, state=state)，返回同样的数据发布接口，并自动加入插件资源登记。插件 unload 时兜底停止服务；stop 后不再重启同一托管实例，需重新 create。普通 SDK 的 Preview 仍可独立使用，其生命周期由调用者负责。
