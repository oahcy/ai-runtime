@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Run script\install.bat first.
    exit /b 1
)
".venv\Scripts\python.exe" -m ai_runtime %*
exit /b %errorlevel%
