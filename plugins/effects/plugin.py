"""Visual smoke-test plugin. Runtime has no dependency on these scenes."""
class Effects:
    def __init__(self, context):
        self.context = context
        self.scenes = {}

    def call(self, action, arguments):
        if action == 'start':
            unknown = set(arguments) - {'scene', 'count'}
            if unknown: raise ValueError(f'Unknown parameters: {sorted(unknown)}')
            name = arguments.get('scene', 'neon')
            if name not in ('neon', 'wind'):
                raise ValueError('scene must be neon or wind')
            if name == 'neon' and 'count' in arguments:
                raise ValueError('count is only supported by wind')
            if name == 'wind' and 'count' in arguments and (type(arguments['count']) is not int or not 10 <= arguments['count'] <= 1500):
                raise ValueError('count must be an integer between 10 and 1500')
            if name not in self.scenes:
                if name == 'neon':
                    from .neon import NeonPreview
                    scene = NeonPreview(context=self.context)
                else:
                    from .wind_tunnel import WindTunnel
                    scene = WindTunnel(count=arguments.get('count', 800), context=self.context)
                self.scenes[name] = scene
            elif 'count' in arguments and len(self.scenes[name].points) != arguments['count']:
                raise ValueError('Stop the scene before changing count')
            result = dict(scene=name, **self.scenes[name].start())
            self.context.logger.info('效果预览已启动', scene=name)
            return result
        if action == 'status':
            if arguments: raise ValueError('status takes no arguments')
            return {name: obj.info() for name, obj in self.scenes.items()}
        if action not in ('configure', 'snapshot', 'stop'):
            raise ValueError('Unknown action')
        allowed = {'scene', 'changes', 'message'} if action == 'configure' else {'scene'}
        if set(arguments) - allowed: raise ValueError('Unknown parameters')
        name = arguments.get('scene')
        if name not in self.scenes: raise ValueError('Start the scene first')
        obj = self.scenes[name]
        if action == 'configure':
            changes = arguments.get('changes', {})
            if not isinstance(changes, dict) or 'message' in changes:
                raise ValueError('changes must be an object; message is a separate parameter')
            fn = obj.update if name == 'neon' else obj.configure
            fn(message=arguments.get('message', 'Plugin scene updated'), **changes)
            self.context.logger.info('效果参数已更新', scene=name, changes=changes)
            return dict(scene=name, **obj.info())
        if action == 'snapshot':
            return obj.snapshot()
        obj.stop()
        del self.scenes[name]
        return {'scene': name, 'stopped': True}

    def close(self):
        for name in tuple(self.scenes):
            self.scenes[name].stop()
            del self.scenes[name]


def create(context):
    return Effects(context)
