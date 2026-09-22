import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Trusted local IPython runtime (not a sandbox)')
    parser.add_argument('--data-dir', type=Path, default=Path('.runtime'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    token_file = root / 'token'
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32), encoding='utf-8')
        if os.name != 'nt':
            token_file.chmod(0o600)
    token = token_file.read_text(encoding='utf-8').strip()
    import uvicorn
    from .observability import configure_logging, LOGGER
    configure_logging(include_server=True)
    LOGGER.info("Trusted local mode; token file: %s", token_file)
    LOGGER.info("API documentation: http://127.0.0.1:%s/docs", args.port)
    from .api import create_app
    uvicorn.run(create_app(root, token, preview_url=f'http://127.0.0.1:{args.port}'), host='127.0.0.1', port=args.port, log_config=None)


if __name__ == '__main__':
    main()
