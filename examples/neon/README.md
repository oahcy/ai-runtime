# 霓虹星轨

在 cwd 为本目录的 Runtime kernel 中执行：

```python
from neon import NeonPreview
scene = NeonPreview()
scene.start()
scene.update(hue=275, particles=1100, speed=1.15, message="紫色星轨，加速")
```

页面、默认参数和校验均属于本示例，主项目只提供 Preview 服务。
参数范围：hue 0–360，speed 0–3，particles 整数 0–2000，rings 整数 1–10，glow 0–1.5，spread 0.5–1.5，title 最多80字符。
浏览器计算动画与鼠标视差，Python 修改场景参数。打开 start 返回的完整 URL，调用 scene.stop() 停止。
