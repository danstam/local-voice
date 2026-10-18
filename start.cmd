:; exec /bin/sh "$(dirname "$0")/scripts/start-posix.sh" "$@"
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\launch.ps1" %*
exit /b %ERRORLEVEL%
