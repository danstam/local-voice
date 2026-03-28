@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
  echo Missing virtualenv Python at %VENV_PYTHON% 1>&2
  echo Set up the project first: 1>&2
  echo   python -m venv .venv 1>&2
  echo   .venv\Scripts\activate 1>&2
  echo   pip install -r requirements.txt 1>&2
  exit /b 1
)

pushd "%SCRIPT_DIR%" >nul
"%VENV_PYTHON%" -m local_voice.app_windows %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%
