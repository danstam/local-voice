@echo off
setlocal

echo.
echo   Welcome to Local Voice
echo   -------------------------
echo   Speech to text, fully offline.
echo.
echo   This installer will:
echo     - Create a Python virtual environment
echo     - Install dependencies (includes PyTorch, ~2 GB)
echo     - Download a Whisper transcription model
echo.
pause
echo.

:: Check Python
where python >nul 2>&1
if errorlevel 1 (
  echo Python is not installed. Please install Python 3.10 or higher from https://www.python.org and try again.
  exit /b 1
)

:: Check Python version 3.10+
for /f "tokens=*" %%v in ('python -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)"') do set py_version=%%v
if %py_version% LSS 310 (
  echo Python 3.10 or higher is required. Please upgrade and try again.
  exit /b 1
)

:: Check ffmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo ffmpeg is not installed. Please run: winget install Gyan.FFmpeg
  exit /b 1
)

:: Create virtual environment
if exist ".venv" (
  echo Virtual environment already exists, skipping.
) else (
  echo Creating virtual environment...
  python -m venv .venv
)

:: Install requirements
echo Installing dependencies...
.venv\Scripts\pip.exe install -r requirements.txt

:: Model selection menu
echo.
echo Select a model to download:
echo.
echo   1) Small   (~461 MB)   -- fast, good for clear English
echo   2) Turbo   (~1.6 GB)   -- best quality/speed ratio for English
echo   3) Medium  (~1.4 GB)   -- best for non-English languages, supports translation
echo   4) Large   (~3.1 GB)   -- highest accuracy, slow on most hardware
echo.
set /p selection=Enter a number [1-4]:

if "%selection%"=="1" set model=small
if "%selection%"=="2" set model=turbo
if "%selection%"=="3" set model=medium
if "%selection%"=="4" set model=large-v3

if not defined model (
  echo Invalid selection.
  exit /b 1
)

echo.
echo Downloading model: %model%
.venv\Scripts\python.exe -m local_voice.app_windows --download-model %model%
if errorlevel 1 (
  echo Model download failed. Check your internet connection and try again.
  exit /b 1
)

echo.
echo Setup complete. Run voice_windows.bat to launch.
endlocal
