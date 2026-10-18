:; exec /bin/sh "$(dirname "$0")/scripts/setup-posix.sh" "$@"
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\bootstrap.ps1" %*
exit /b %ERRORLEVEL%
