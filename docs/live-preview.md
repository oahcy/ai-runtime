# 通用 Live Preview

主项目负责本机 HTTP / WebSocket 服务、凭证校验、JSON 状态、版本和最近30条更新记录。
页面、业务默认值及业务校验由使用方提供。运行中的 kernel 与 MCP 客户端连接独立；关闭、重建或异常退出 kernel 会撤销其预览。

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

html 为可信本机代码，在实例创建时读取，不自动热替换。Runtime 自动在页面脚本前注入客户端接口，只托管传入的页面，不托管任意文件目录。HTML 可以直接使用内存字符串，不需要写文件。

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

在 ai-runtime 管理的 kernel 中，页面统一挂到主服务的 /previews/{id}/#凭证，共用服务端口（默认 8765），不为每个预览另开 HTTP 监听端口。凭证独立于 Runtime 主 token，通过 URL fragment 提供。
WebSocket 校验 Origin 和凭证，不提供浏览器执行 Python 的接口。
重连立即获得完整最新快照；50ms 轮询检测状态变化，快速更新可能合并。
info() 获取链接，snapshot() 读取状态，stop() 停止预览。状态不自动持久化。
从旧版迁移时，注册 effects 插件后在新 kernel 使用 runtime.plugins.load("effects")；已有 kernel 中载入的旧类不会自动热更新。


## 插件使用托管预览

在插件中优先使用 context.preview.create(html=html, state=state)，返回同样的数据发布接口，并自动加入插件资源登记。插件 unload 时兜底撤销预览；stop 后不再重启同一托管实例，需重新 create。普通 SDK 的 Preview 仍可独立使用，其生命周期由调用者负责。

## 主服务托管与生命周期

- 主服务负责网页、浏览器连接、鉴权和最新状态缓存；kernel 仅通过内部发布连接发送 HTML 与 JSON 状态。
- 插件继续使用 context.preview.create → start → publish/update → stop。传输细节对插件透明。
- start() 等待主服务确认登记后返回链接；info() 返回 mode="hosted"、运行状态和链接。托管模式不接受独立 port 参数。
- 每个会话最多 16 个预览，HTML 上限 2 MiB，快照上限 4 MiB。超限登记失败；运行中超限会关闭发布连接，info() 显示错误。
- 发布凭证限定于会话当前代次，重启后旧凭证失效。stop、插件卸载、会话关闭、kernel 丢失都会清除对应网页；发布连接断开也撤销网页。
- HTML、快照和浏览器凭证只在内存中保存；执行提交的 Python 代码仍按 Runtime 既有机制进入执行历史。网页不会获得主 API token 或内部发布凭证。
- 发布连接失败后不自动重建预览；查看 info() 后停止旧实例并创建新实例。浏览器短暂断线仍可自动重连到存活的预览。
- 在 ai-runtime 以外直接使用 Preview 时，保留独立本机随机端口模式，生命周期由调用者负责。嵌入式部署可使用 create_app(..., preview_url="http://127.0.0.1:端口") 指定主服务地址。

升级后需重启 ai-runtime 服务并创建新会话。旧 kernel 中已导入的类不会自动切换托管方式。
