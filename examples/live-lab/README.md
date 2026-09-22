# FLOW / 实时粒子风洞

通过 ai-runtime 的持久 Python kernel 运行，不额外安装依赖。

在 cwd 为本目录的 kernel 中执行：

```python
from lab import WindTunnel
lab = WindTunnel()
lab.start()  # 打开返回的完整 URL
```

在同一个 session 中持续控制：

```python
lab.configure(vortices=[[370,210,180],[650,390,-180]], message="加入两个反向旋涡")
lab.configure(wind=[120,-70], message="风向转向右上方")
lab.configure(obstacles=[], message="移走障碍物")
lab.configure(paused=True, message="暂停 Python 模拟")
lab.configure(paused=False, message="恢复 Python 模拟")
lab.stop()
```

坐标范围 1000×600，y 向下。wind 为二维目标速度（每维 -250～250）；vortices 每项为 [x,y,强度]，强度 -250～250；obstacles 每项 [x,y,半径]，半径 10～120。每类最多 8 个物体。

Python 后台线程按固定 dt=1/30 计算速度松弛、旋涡场与圆障碍物碰撞。边界周期回绕。WebSocket 约 20Hz 推送真实位置，浏览器只绘制轨迹、网格和曲线，不计算粒子运动。若机器负载过高，模拟时间可能慢于真实时间。

浏览器的“冻结观察”仅冻结画面；Python 和指标仍更新。使用 paused=True 才暂停 Python。操作记录展示最近20条命令；断线重连恢复最新完整状态。曲线保存在浏览器内存，刷新会清空。此项目复用 ai_runtime.preview.Preview 的本机监听、Origin 校验及独立凭证，不提供浏览器执行 Python 的接口。

这是交互式粒子场示例，不是工程流体力学或 CFD 求解器。kernel 关闭后预览停止；MCP 断开则继续运行。

测试：使用 ai-runtime 虚拟环境运行 `python -m pytest test_lab.py -q`。
