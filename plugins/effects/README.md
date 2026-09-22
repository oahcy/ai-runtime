# effects：效果测试插件

用来检验 ai-runtime 的持久实例、动作调用、后台任务和实时预览。不是 Runtime 的依赖。
安装/接口见 [Runtime 插件说明](../../docs/plugins.md)。

两个 scene：
- neon：浏览器计算动画，Python 修改参数。
- wind：Python 后台计算粒子运动，WebSocket 持续推送。

先注册本目录，再在任意 cwd 的 Runtime 会话使用 runtime.plugins.load("effects")。

动作：start、configure、snapshot、status、stop，参数见 plugin.json。
neon 参数：hue 0–360，speed 0–3，particles 整数0–2000，rings 整数1–10，glow 0–1.5，spread 0.5–1.5，title 最多80字符。
wind 参数：wind [vx,vy]，每维 -250～250；vortices [[x,y,power]]，power -250～250；obstacles [[x,y,radius]]，radius 10～120；paused 布尔值。坐标1000×600，y向下，每类最多8个物体。
start 的 count 只用于 wind 首次创建，整数10～1500。要改变 count 先 stop。
configure 的 changes 不含 message，message 为外层字段。
status 返回各场景 URL；snapshot 返回完整状态（风洞含所有粒子，输出较大）。
unload effects 会关闭两个场景。网页自身的暂停/冻结按钮只暂停本地观察，风洞 Python 是否暂停由 paused 决定。

本插件是效果与功能验证示例，风洞不是工程 CFD 求解器。
