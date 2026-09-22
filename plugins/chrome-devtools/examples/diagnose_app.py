"""Run in a Runtime kernel. Produces diagnostics, never retries or restarts the app."""

def diagnose_app(runtime, package, debugger_url, *, serial=None, release_wait=False):
    adb = runtime.plugins.load('adb')
    dev = runtime.plugins.load('chrome-devtools')
    report = {}
    # Start with logs: a missing resource can explain startup without JS evaluation.
    try:
        args = {'package': package, 'timeout': 12}
        if serial is not None:
            args['serial'] = serial
        report['device'] = adb.call('diagnose', args)
    except Exception as exc:
        report['device'] = {'error': type(exc).__name__}
    try:
        connection = dev.call('connect', {'url': debugger_url, 'timeout': 4})
        report['debugger'] = dev.call('prepare', {'release_wait': release_wait, 'timeout': 6})
        report['events'] = dev.call('events', {'after': connection['cursor'], 'limit': 20})
    except Exception as exc:
        report['debugger'] = {'error': type(exc).__name__, 'diagnostics': dev.call('status')}
    # Keep successful connections for follow-up debugging. No implicit resume.
    return report
