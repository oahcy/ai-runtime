"""Concise daemon logs plus kernel-to-daemon plugin lifecycle events."""
import json
import logging
import os
import re
import sys

LOGGER = logging.getLogger('ai_runtime')
MIME = 'application/vnd.ai-runtime.log+json'
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|api[_-]?key', re.I)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        record.source = {"ai_runtime": "runtime", "uvicorn": "server", "uvicorn.error": "server", "uvicorn.access": "http"}.get(record.name, record.name)
        return super().format(record)


def configure_logging(include_server=False):
    if not any(getattr(h, '_runtime_handler', False) for h in LOGGER.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._runtime_handler = True
        handler.setFormatter(ConsoleFormatter('%(asctime)s %(levelname)-7s [%(source)s] %(message)s', '%H:%M:%S'))
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
    else:
        log_event(event, **fields)
