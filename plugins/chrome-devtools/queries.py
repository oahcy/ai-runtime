"""Small CDP conveniences. Source and breakpoint line numbers are one-based."""
def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def nonempty(value, name):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f'{name} must be a nonempty string up to 4096 characters')
    return value


PARAMETERS = {
    'scripts': {'query', 'offset', 'limit', 'session_id'},
    'source': {'script_id', 'start_line', 'line_count', 'timeout', 'session_id'},
    'stack': {'session_id'},
    'breakpoint_set': {'url', 'line', 'column', 'condition', 'timeout', 'session_id'},
    'breakpoint_remove': {'breakpoint_id', 'timeout', 'session_id'},
    'resume': {'timeout', 'session_id'},
}


def query(dev, action, a):
    session = a.get('session_id')
    if session is not None:
        nonempty(session, 'session_id')
    key = session or 'default'
    seconds = a.get('timeout', 10)
    if action == 'scripts':
        text = a.get('query', '')
        if not isinstance(text, str):
            raise ValueError('query must be a string')
        offset = integer(a.get('offset', 0), 'offset', 0, 100000)
        limit = integer(a.get('limit', 50), 'limit', 1, 200)
        with dev.lock:
            rows = [dict(v) for (s, _), v in dev.scripts.items() if s == key and text.lower() in v['url'].lower()]
            return {'scripts': rows[offset:offset+limit], 'total': len(rows),
                    'has_more': offset+limit < len(rows), 'cache_truncated': dev.scripts_truncated,
                    'connected': dev.ws is not None}
    if action == 'stack':
        with dev.lock:
            return {'connected': dev.ws is not None, 'execution': dev.protocol.get(key, 'unknown'),
                    'pause': dev.stacks.get(key)}
    if action == 'source':
        script = nonempty(a.get('script_id'), 'script_id')
        start = integer(a.get('start_line', 1), 'start_line', 1, 10000000)
        count = integer(a.get('line_count', 80), 'line_count', 1, 300)
        result = dev._command('Debugger.getScriptSource', {'scriptId': script}, seconds, session)
        lines = result.get('scriptSource', '').splitlines()
        selected = '\n'.join(lines[start-1:start-1+count])
        return {'script_id': script, 'start_line': start, 'total_lines': len(lines),
                'source': selected[:65536], 'truncated': len(selected) > 65536,
                'has_more': start-1+count < len(lines)}
    if action == 'breakpoint_set':
        params = {'url': nonempty(a.get('url'), 'url'),
                  'lineNumber': integer(a.get('line'), 'line', 1, 10000000)-1,
                  'columnNumber': integer(a.get('column', 1), 'column', 1, 10000000)-1}
        if 'condition' in a:
            if not isinstance(a['condition'], str) or len(a['condition']) > 4096:
                raise ValueError('condition must be a string up to 4096 characters')
            params['condition'] = a['condition']
        return dev._command('Debugger.setBreakpointByUrl', params, seconds, session)
    if action == 'breakpoint_remove':
        return dev._command('Debugger.removeBreakpoint', {'breakpointId': nonempty(a.get('breakpoint_id'), 'breakpoint_id')}, seconds, session)
    return dev._command('Debugger.resume', {}, seconds, session)
