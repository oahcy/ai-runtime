"""Small CDP conveniences. Source and breakpoint line numbers are one-based."""
import time
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
    'pause': {'timeout', 'session_id'},
    'step_over': {'timeout', 'session_id'},
    'step_into': {'timeout', 'session_id'},
    'step_out': {'timeout', 'session_id'},
    'source_search': {'query', 'script_id', 'url_query', 'offset', 'max_scripts', 'limit', 'case_sensitive', 'timeout', 'session_id'},
    'variables': {'object_id', 'offset', 'limit', 'timeout', 'session_id'},
    'frame_evaluate': {'call_frame_id', 'expression', 'allow_side_effects', 'timeout', 'session_id'},
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
    if action == 'source_search':
        from .plugin import timeout
        needle = nonempty(a.get('query'), 'query')
        sensitive = a.get('case_sensitive', False)
        if type(sensitive) is not bool:
            raise ValueError('case_sensitive must be boolean')
        offset = integer(a.get('offset', 0), 'offset', 0, 100000)
        count = integer(a.get('max_scripts', 20), 'max_scripts', 1, 100)
        limit = integer(a.get('limit', 100), 'limit', 1, 500)
        url_query = a.get('url_query', '')
        if not isinstance(url_query, str):
            raise ValueError('url_query must be a string')
        with dev.lock:
            rows = [dict(v) for (k, _), v in dev.scripts.items() if k == key and url_query.lower() in v['url'].lower()]
        if 'script_id' in a:
            script_id = nonempty(a['script_id'], 'script_id')
            with dev.lock:
                rows = [dict(dev.scripts.get((key, script_id), {'script_id': script_id, 'url': ''}))]
        deadline = time.monotonic() + timeout(seconds)
        matches, errors, scanned, truncated = [], [], 0, False
        for row in rows[offset:offset+count]:
            remaining = deadline - time.monotonic()
            if remaining < .1:
                break
            try:
                result = dev._command('Debugger.searchInContent', {
                    'scriptId': row['script_id'], 'query': needle,
                    'caseSensitive': sensitive, 'isRegex': False}, remaining, session)
            except (RuntimeError, TimeoutError) as exc:
                errors.append({'script_id': row['script_id'], 'error': str(exc)[:500]})
                scanned += 1
                break
            scanned += 1
            for hit in result.get('result', []):
                if len(matches) >= limit:
                    truncated = True
                    break
                text = hit.get('lineContent', '')
                matches.append({'script_id': row['script_id'], 'url': row['url'],
                                'line': hit['lineNumber']+1, 'text': text[:2000],
                                'text_truncated': len(text) > 2000})
            if truncated:
                break
        return {'matches': matches, 'errors': errors, 'scanned': scanned, 'next_offset': offset+scanned,
                'has_more': offset+scanned < len(rows), 'truncated': truncated,
                'note': 'Literal search in loaded generated JavaScript; if truncated, narrow query or script_id.'}
    if action == 'variables':
        object_id = nonempty(a.get('object_id'), 'object_id')
        offset = integer(a.get('offset', 0), 'offset', 0, 100000)
        limit = integer(a.get('limit', 50), 'limit', 1, 200)
        result = dev._command('Runtime.getProperties', {
            'objectId': object_id, 'ownProperties': True, 'generatePreview': False}, seconds, session)
        properties = result.get('result', [])
        return {'properties': [compact_property(p) for p in properties[offset:offset+limit]],
                'total': len(properties), 'has_more': offset+limit < len(properties),
                'next_offset': min(offset+limit, len(properties)), 'exceptionDetails': result.get('exceptionDetails')}
    if action == 'frame_evaluate':
        frame = nonempty(a.get('call_frame_id'), 'call_frame_id')
        expression = nonempty(a.get('expression'), 'expression')
        allow = a.get('allow_side_effects', False)
        if type(allow) is not bool:
            raise ValueError('allow_side_effects must be boolean')
        return dev._command('Debugger.evaluateOnCallFrame', {
            'callFrameId': frame, 'expression': expression, 'returnByValue': False,
            'generatePreview': False, 'throwOnSideEffect': not allow}, seconds, session)
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
    commands = {'resume': 'Debugger.resume', 'pause': 'Debugger.pause',
                'step_over': 'Debugger.stepOver', 'step_into': 'Debugger.stepInto', 'step_out': 'Debugger.stepOut'}
    return dev._command(commands[action], {}, seconds, session)


def remote_summary(value):
    if not isinstance(value, dict):
        return value
    result = {k: value[k] for k in ('type', 'subtype', 'className', 'objectId', 'unserializableValue') if k in value}
    for k in ('value', 'description'):
        if k in value:
            v = value[k]
            if isinstance(v, str) and len(v) > 2000:
                result[k] = v[:2000]
                result['truncated'] = True
            else:
                result[k] = v
    return result


def compact_property(prop):
    result = {k: prop[k] for k in ('name', 'enumerable', 'configurable', 'writable', 'isOwn', 'wasThrown') if k in prop}
    for k in ('value', 'get', 'set', 'symbol'):
        if k in prop:
            result[k] = remote_summary(prop[k])
    return result
