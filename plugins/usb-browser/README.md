# USB 手机浏览器调试

手机开启 USB 调试并授权电脑，打开 Chrome 页面。电脑需要 Android platform-tools。
此插件复用同目录下 adb 和 chrome-devtools 的实现，创建独立实例，日志、截图产物和资源归 usb-browser 管理；无需先加载两个插件。分发时保留这两个兄弟目录。

```python
usb = runtime.plugins.load('usb-browser')
usb.call('devices')
usb.call('browsers', {'serial': '手机序列号'})
usb.call('open', {'serial': '手机序列号', 'socket': 'chrome_devtools_remote'})
usb.call('connect', {'target_id': 'open 返回的页面 ID'})
usb.call('cdp', {'action': 'evaluate', 'arguments': {'expression': 'document.title'}})
usb.call('cdp', {'action': 'screenshot'})
# 触摸坐标为页面视口 CSS 像素；一次点击需要 start 和 end。
usb.call('cdp', {'action': 'command', 'arguments': {'method': 'Input.dispatchTouchEvent',
    'params': {'type': 'touchStart', 'touchPoints': [{'x': 100, 'y': 200}]}}})
usb.call('cdp', {'action': 'command', 'arguments': {'method': 'Input.dispatchTouchEvent',
    'params': {'type': 'touchEnd', 'touchPoints': []}}})
usb.call('disconnect')
```

只有一个设备、浏览器或页面时可省略相应选择参数；多个候选必须明确选择。
WebView 必须由宿主启用调试；插件不会修改手机设置、启动应用或自动重连、重放命令。
浏览器发现读取 /proc/net/unix；若设备限制访问，调用会报告错误。
ADB 已连接的无线设备也可显式选择；本插件不负责配对。

每次 open 使用 tcp:0 自动分配独立端口，不覆盖既有转发。页面地址的主机部分固定改写为自身转发端口。
连接失败可检查 status/targets 后显式重试。disconnect 或卸载关闭连接并清理自身转发，不停止共享 ADB server。
手机拔出或清理失败会保留资源记录供重试。Runtime 进程被强杀时可能遗留转发，可通过 adb forward --list 检查。

注册：`.venv/Scripts/python.exe -m ai_runtime.plugins add D:/prj/ai-runtime/plugins/usb-browser`
