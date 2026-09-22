"""Event-driven pause waits and bounded, read-only pause summaries."""
import math
import time
from .queries import integer

PARAMETERS = {
    'wait_for_pause': {'wait_seconds', 'after_pause', 'session_id', 'summary', 'timeout',
                       'frame_limit', 'variable_limit', 'source_lines'},
    'pause_snapshot': {'session_id', 'timeout', 'frame_limit', 'variable_limit', 'source_lines'},
}


def snapshot(dev, a, expected=None):
    from .plugin import timeout
    key = a.get('session_id') or 'default'
    frame_limit = integer(a.get('frame_limit', 5), 'frame_limit', 1, 20)
    variable_limit = integer(a.get('variable_limit', 12), 'variable_limit', 0, 50)
    source_lines = integer(a.get('source_lines', 7), 'source_lines', 0, 31)
    budget = timeout(a.get('timeout', 3))
    deadline = time.monotonic() + budget
    with dev.lock:
        pause = dev.stacks.get(key)
        ws = dev.ws
        if pause is None or (expected is not None and pause is not expected):
            return {'status': 'not_paused' if expected is None else 'state_changed', 'pause_id': None}
        result = {'status': 'paused', 'pause_id': pause['pause_id'], 'reason': pause['reason'],
                  'frames': [{k: f.get(k) for k in ('call_frame_id', 'function', 'url', 'script_id', 'line', 'column')}
                             for f in pause['frames'][:frame_limit]],
                  'frames_truncated': pause['truncated'] or len(pause['frames']) > frame_limit,
                  'variables': [], 'source': None, 'errors': []}
    def current():
        with dev.lock:
            return dev.ws is ws and dev.stacks.get(key) is pause

    def command(method, params):
        if not current():
            raise RuntimeError('Pause changed during inspection')
        remaining = deadline-time.monotonic()
        if remaining < .1:
            raise TimeoutError('Inspection budget exhausted')
        return dev._command(method, params, remaining, a.get('session_id'))

    frames = pause['frames']
    if frames:
        top = frames[0]
        local = next((s for s in top['scopes'] if s['type'] == 'local'), None)
        if variable_limit and local and local.get('object', {}).get('objectId'):
            try:
                raw = command('Runtime.getProperties', {'objectId': local['object']['objectId'],
                              'ownProperties': True, 'generatePreview': False})
                if raw.get('exceptionDetails'):
                    result['errors'].append({'part': 'variables', 'error': 'getProperties returned exceptionDetails'})
                props = raw.get('result', [])
                for prop in props[:variable_limit]:
                    value = prop.get('value', {})
                    item = {'name': prop.get('name', '')[:200], 'type': value.get('type', 'accessor')}
                    for k in ('objectId', 'unserializableValue'):
                        if k in value: item[k] = value[k]
                    if 'value' in value:
                        v = value['value']
                        item['value'] = v[:200] if isinstance(v, str) else v
                        if isinstance(v, str) and len(v) > 200: item['truncated'] = True
                    elif value.get('description'):
                        item['description'] = value['description'][:200]
                    result['variables'].append(item)
                result['variables_total'] = len(props)
                result['variables_truncated'] = len(props) > variable_limit
                result['scope_object_id'] = local['object']['objectId']
            except (RuntimeError, TimeoutError, ValueError) as exc:
                result['errors'].append({'part': 'variables', 'error': str(exc)[:300]})
        if source_lines and top.get('script_id'):
            try:
                raw = command('Debugger.getScriptSource', {'scriptId': top['script_id']})
                lines = raw.get('scriptSource', '').splitlines()
                start = max(1, top['line']-source_lines//2)
                selected = lines[start-1:start-1+source_lines]
                result['source'] = {'start_line': start,
                    'lines': [{'line': start+i, 'text': line[:300], 'current': start+i == top['line'],
                               'truncated': len(line)>300} for i,line in enumerate(selected)]}
            except (RuntimeError, TimeoutError, ValueError) as exc:
                result['errors'].append({'part': 'source', 'error': str(exc)[:300]})
    if not current():
        return {'status': 'state_changed', 'pause_id': pause['pause_id'],
                'message': 'Target resumed, re-paused or disconnected during inspection; fetch a fresh snapshot.'}
    result['partial'] = bool(result['errors'])
    return result


def inspect(dev, action, a):
    session = a.get('session_id')
    if session is not None and (not isinstance(session, str) or not session):
        raise ValueError('session_id must be a nonempty CDP session string')
    # Validate inspection options before waiting or issuing any protocol command.
    integer(a.get('frame_limit', 5), 'frame_limit', 1, 20)
    integer(a.get('variable_limit', 12), 'variable_limit', 0, 50)
    integer(a.get('source_lines', 7), 'source_lines', 0, 31)
    from .plugin import timeout
    timeout(a.get('timeout', 3))
    if action == 'pause_snapshot':
        return snapshot(dev, a)
    seconds = a.get('wait_seconds', 5)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 20:
        raise ValueError('wait_seconds must be 0..20')
    after = integer(a.get('after_pause', 0), 'after_pause', 0, 2**63-1)
    summary = a.get('summary', True)
    if type(summary) is not bool:
        raise ValueError('summary must be boolean')
    key = session or 'default'
    deadline = time.monotonic()+seconds
    with dev.changed:
        ws = dev.ws
        while True:
            if ws is None or dev.ws is not ws:
                return {'status': 'disconnected', 'pause_id': None}
            pause = dev.stacks.get(key)
            if pause and pause['pause_id'] > after:
                break
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return {'status': 'timeout', 'pause_id': None, 'execution': dev.protocol.get(key, 'unknown')}
            dev.changed.wait(remaining)
    if summary:
        return snapshot(dev, a, expected=pause)
    return {'status': 'paused', 'pause_id': pause['pause_id'], 'reason': pause['reason']}
