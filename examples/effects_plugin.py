"""Run this file's contents inside an ai-runtime kernel after registering effects."""
fx = runtime.plugins.load('effects')
print(fx.call('start', {'scene': 'neon'}))
print(fx.call('configure', {
    'scene': 'neon', 'changes': {'hue': 275}, 'message': 'Plugin test: purple'
}))
# Keep the same session to continue:
# fx.call('start', {'scene': 'wind', 'count': 800})
# runtime.plugins.unload('effects')
