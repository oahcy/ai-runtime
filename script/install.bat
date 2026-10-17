@echo off
setlocal
if defined AI_RUNTIME_PYTHON (
    "%AI_RUNTIME_PYTHON%" "%~dp0install.py" %*
    exit /b
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0install.py" %*
    exit /b
)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    python "%~dp0install.py" %*
    exit /b
)
echo ERROR: Install Python 3.11+ or set AI_RUNTIME_PYTHON to its executable path.
exit /b 1
