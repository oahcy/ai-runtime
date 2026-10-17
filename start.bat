@echo off
setlocal
call "%~dp0script\start.bat" %*
set "runtimeExitCode=%errorlevel%"
if not "%runtimeExitCode%"=="0" (
    echo.
    echo AI Runtime exited with code %runtimeExitCode%.
    pause
)
exit /b %runtimeExitCode%
