# Game Agent：无 eval 的小游戏对象调试

实验版 v0.2。游戏端使用普通 CommonJS 模块和 `wx.connectSocket`，不使用 eval 或 Function 构造器。支持对象读写、方法调用和 console 日志；不提供 CDP 的断点、单步或局部变量。微信真机兼容性仍需实测。

## 接入

安装脚本自动注册；已有项目可执行：

```powershell
.\.venv\Scripts\python.exe -m ai_runtime.plugins add ./plugins/game-agent
```

将本目录 `agent.js` 复制到小游戏工程，命名为 `ai-runtime-agent.js`，随游戏打包。它不能通过下载文本自动执行。

在 Runtime 持久会话启动插件：

```python
agent = runtime.plugins.load('game-agent')
pairing = agent.call('start', {'host': '192.168.2.80'})
print(pairing['loader'])
```

替换为电脑实际局域网 IPv4。返回 `config`、本地 `agent_file`、用于下载模块文件的 `url` 和入口 `loader`。把 loader 放进游戏入口，它形如：

```js
const aiAgent = require('./ai-runtime-agent.js')({url:'ws://电脑IP:端口/ws', token:'配对凭证'});
// 在引擎初始化完成、能访问 cc 的位置注册：
aiAgent.register('cc', cc);
// 也可注册当前场景、业务对象；名字相同会替换入口。
aiAgent.register('scene', cc.director.getScene());
```

不要假定 `cc` 一定是全局变量，使用工程中实际的引擎引用。切换场景后重新注册 scene。只有显式注册的入口会列在 roots 中，但通过这些对象调用方法仍能产生真实业务影响。

手机必须能访问电脑监听地址。平台网络规则和防火墙仍然适用。默认 host 为 127.0.0.1；port 默认自动分配。服务独立于 8765 主 API，由插件资源管理器清理。

配对凭证五分钟内有效，不要提交到发行包。`pair` 会生成新配置；已有连接保持。HTTP/WS 仅适合可信局域网，无传输加密。游戏连接断开后不自动重连，需 `aiAgent.close()` 后重新调用模块导出的函数，不能仅再次 require（模块会缓存）。

## 操作协议

```python
target = agent.call('status', {})['targets'][0]['target_id']  # 多目标请明确选择

def op(command):
    return agent.call('operate', {'target_id': target, 'command': command})

roots = op({'op': 'roots'})['value']
scene = next(r['object']['$ref'] for r in roots if r['name'] == 'scene')
op({'op': 'keys', 'object': scene})
op({'op': 'get', 'object': scene, 'property': 'name'})
op({'op': 'set', 'object': scene, 'property': 'name', 'value': {'value': 'Debug Scene'}})
op({'op': 'call', 'object': scene, 'method': 'getChildByName', 'args': [{'value': 'Canvas'}]})
op({'op': 'release', 'object': scene})
agent.call('events', {'after': 0})
agent.call('stop', {})
```

MCP 使用 `call_plugin_and_wait`，action 为 `operate`。旧 `evaluate` 已删除；`evaluate_js` 仍只用于 CDP 插件。

- `roots` 返回注册入口及其对象描述。
- `get` 返回原始值描述或对象句柄，不遍历对象。显式读取会执行 getter。
- `keys` 只列自身属性名，不触发普通 getter；每页100个，传 offset 继续。代理对象仍可能触发自身代理逻辑。
- `set` 的 value、`call` 的每个 args 元素须为 `{value: JSON值}` 或 `{$ref: 对象ID}`。
- `call` 保留接收对象的 this，等待 Promise。参数最多64个。返回对象可继续读取和调用。
- `release` 释放句柄；旧 ID 随即失效。注册入口仍保留对象，再次 roots 会分配新 ID。

原始值结果例如 `{type:'number', value:42}`；对象结果例如 `{type:'object', $ref:'o1', array:false}`。undefined、非有限数等使用 type/text 描述。字符串最多8000字符；禁止通过协议访问 constructor、prototype、__proto__。这不是隔离沙箱。

对象上限2048，注册入口上限64。console 每秒最多转发20条，对象参数返回句柄。事件历史200条，通过 seq 游标读取，过旧游标标记 truncated。每个连接最多256个独立指令，保留请求 ID 防止重复执行，满后需重新连接。关闭会释放句柄并恢复仍由插件持有的 console 包装。

超时表示结果未知，不能中止已执行的方法或撤销修改；检查实际状态后再决定下一步，不要自动重放写操作。新版本需要重新加载 Python 插件和游戏端模块。不要混用 v0.1 的 eval 加载器。
