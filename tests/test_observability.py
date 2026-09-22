import logging
from ai_runtime.observability import short, code_summary, log_event, configure_logging, LOGGER


def test_log_summaries_redact_and_bound(caplog):
    caplog.set_level(logging.INFO, logger='ai_runtime')
    log_event('test', token='private', arguments={'password':'private', 'nested': {'api_key':'private'}, 'url':'http://localhost:1/#private'}, code=code_summary('api_key = "private"\nprint(42)'))
    text = caplog.records[-1].getMessage()
    assert 'private' not in text
    assert 'print(42)' in text
    assert '<redacted>' in text
    assert len(short('x'*1000)) <= 281
    log_event('test\nforged', request='line1\nline2')
    assert '\n' not in caplog.records[-1].getMessage()


def test_logging_setup_does_not_duplicate_handlers():
    configure_logging()
    configure_logging()
    assert sum(bool(getattr(h, '_runtime_handler', False)) for h in LOGGER.handlers) == 1
