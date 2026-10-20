"""Shared installer used by install.bat and install.sh (standard library only)."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description="Install ai-runtime into the project's .venv")
    parser.add_argument('--dev', action='store_true', help='Also install test dependencies')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11+ is required. Install it and select it with AI_RUNTIME_PYTHON.')
    root = Path(__file__).resolve().parent.parent
    environment = root / '.venv'
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if environment.exists() and not python.is_file():
        parser.error(f'{environment} exists but has no usable Python. Inspect it before retrying; it was not removed.')
    if not environment.exists():
        print(f'Creating virtual environment: {environment}', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(python), '-c',
                    'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Existing .venv requires Python 3.11+")'],
                   check=True)
    extras = 'mcp,test' if args.dev else 'mcp'
    subprocess.run([str(python), '-m', 'pip', 'install', '-e', f'.[{extras}]'], cwd=root, check=True)
    for plugin in ('effects', 'chrome-devtools', 'adb', 'usb-browser', 'game-agent'):
        subprocess.run([str(python), '-m', 'ai_runtime.plugins', '--data-dir', str(root / '.runtime'),
                        'add', str(root / 'plugins' / plugin)], cwd=root, check=True)
    print('Installed. Bundled plugins registered in .runtime (not loaded).')
    print('Start with script\\start.bat on Windows or bash script/start.sh on macOS.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'Installation failed: {exc}', file=sys.stderr)
        sys.exit(1)
