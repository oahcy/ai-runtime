"""Concise daemon logs plus kernel-to-daemon plugin lifecycle events."""
import json
from copy import copy
from datetime import datetime, timezone
import logging
import os
import re
import sys

LOGGER = logging.getLogger('ai_runtime')
MIME = 'application/vnd.ai-runtime.log+json'
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|api[_-]?key', re.I)


class ConsoleFormatter(logging.Formatter):
    COLORS = {logging.DEBUG: 36, logging.INFO: 32, logging.WARNING: 33,
              logging.ERROR: 31, logging.CRITICAL: 91}

    def __init__(self, *args, use_colors=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_colors = (sys.stderr.isatty() and 'NO_COLOR' not in os.environ
                           and os.environ.get('TERM') != 'dumb') if use_colors is None else use_colors

    def format(self, record):
        record = copy(record)
        if hasattr(record, "runtime_log"):
            record.created = datetime.fromisoformat(record.runtime_log["time"]).timestamp()
            record.msecs = (record.created % 1) * 1000
        level = record.levelname
        color = self.COLORS.get(record.levelno)
        label = f"\033[{color}m{level}\033[0m" if self.use_colors and color else level
        record.level_label = label + ' ' * max(0, 7 - len(level))
        record.source = {"ai_runtime": "runtime", "uvicorn": "server", "uvicorn.error": "server", "uvicorn.access": "http"}.get(record.name, record.name)
        return super().format(record)


def configure_logging(include_server=False):
    if not any(getattr(h, '_runtime_handler', False) for h in LOGGER.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._runtime_handler = True
        handler.setFormatter(ConsoleFormatter('%(asctime)s %(level_label)s [%(source)s] %(message)s', '%H:%M:%S'))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    if include_server:
        handler = next(h for h in LOGGER.handlers if getattr(h, "_runtime_handler", False))
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers = [handler]
            logger.setLevel(logging.INFO)
            logger.propagate = False


def clean(value):
    if isinstance(value, dict):
        return {str(k): '<redacted>' if SENSITIVE.search(str(k)) else clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value[:20]]
    if isinstance(value, str):
        # Preview URL fragments are capability tokens, not useful diagnostic content.
        return re.sub(r'(https?://[^\s\x22\x27<>#]+)#[^\s\x22\x27<>]*', r'\1#<redacted>', value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def short(value, limit=280):
    text = json.dumps(clean(value), ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + '…'


def code_summary(code):
    # Omit lines mentioning credential fields rather than printing their values.
    lines = ['<credential-related line omitted>' if SENSITIVE.search(line) else line.strip()
             for line in code.splitlines() if line.strip()]
    return ' ; '.join(lines)


def log_event(event, *, level=logging.INFO, **fields):
    fields = clean(fields)
    label = re.sub(r'[^a-zA-Z0-9_.-]', '_', str(event))[:80]
    LOGGER.log(level, '%s %s', label, ' '.join(f'{re.sub(r"[^a-zA-Z0-9_]", "_", str(k))}={short(v)}' for k,v in fields.items()))


def plugin_event(event, **fields):
    payload = {'event': event, 'fields': clean(fields)}
    if os.environ.get('AI_RUNTIME_SESSION_DIR'):
        # IOPub already has the executing request's parent ID.
        from IPython.display import display
        display({MIME: payload}, raw=True)
    elif event == "plugin.log":
        relay_plugin_log(payload["fields"])
    else:
        log_event(event, **fields)

class PluginLogger:
    """Structured business logger bound to a plugin by PluginManager."""
    def __init__(self, plugin_id):
        self.plugin_id = plugin_id

    def log(self, level, msg, **fields):
        if isinstance(level, str):
            level = level.upper()
        elif type(level) is int:
            level = logging.getLevelName(level)
        if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            raise ValueError('Unsupported log level')
        if not isinstance(msg, str):
            raise TypeError('msg must be a string')
        plugin_event('plugin.log', time=datetime.now(timezone.utc).isoformat(),
                     level=level, msg=clean(msg), plugin=self.plugin_id, fields=clean(fields))

    def debug(self, msg, **fields):
        self.log('DEBUG', msg, **fields)

    def info(self, msg, **fields):
        self.log('INFO', msg, **fields)

    def warning(self, msg, **fields):
        self.log('WARNING', msg, **fields)

    def error(self, msg, **fields):
        self.log('ERROR', msg, **fields)

    def critical(self, msg, **fields):
        self.log('CRITICAL', msg, **fields)


def relay_plugin_log(fields, session_id=None, execution_id=None):
    """Validate the transport record; bad plugin messages must not kill the kernel reader."""
    if not isinstance(fields, dict):
        return None
    if fields.get('level') not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        return None
    if not all(isinstance(fields.get(k), str) for k in ('time', 'msg', 'plugin')):
        return None
    if not isinstance(fields.get('fields'), dict):
        return None
    try:
        emitted = datetime.fromisoformat(fields['time'])
        if emitted.tzinfo is None:
            return None
        emitted.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    entry = clean({k: fields[k] for k in ('time', 'level', 'msg', 'plugin', 'fields')})
    entry.update(session_id=session_id, execution_id=execution_id)
    LOGGER.log(getattr(logging, entry['level']),
               'plugin.log plugin=%s session=%s execution=%s msg=%s fields=%s',
               short(entry['plugin']), short(session_id[:8] if session_id else None),
               short(execution_id[:8] if execution_id else None), short(entry['msg']), short(entry['fields']),
               extra={'runtime_log': entry})
    return entry
