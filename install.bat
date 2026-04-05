@echo off
setlocal EnableDelayedExpansion

echo.
echo   Welcome to Local Voice
echo   -------------------------
echo   Speech to text, fully offline.
echo.
echo   This installer will:
echo     - Install Python 3.12 if needed
echo     - Install ffmpeg if needed
echo     - Detect your hardware and install the right version of PyTorch
echo     - Install dependencies (includes PyTorch, ~2 GB)
echo     - Download a Whisper transcription model
echo.
pause
echo.

where python >nul 2>&1
if errorlevel 1 goto no_python
for /f "tokens=*" %%v in ('python -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)"') do set py_version=%%v
if !py_version! GEQ 310 goto python_ok

:no_python
echo Python 3.10 or higher not found. Installing Python 3.12...
where winget >nul 2>&1
if not errorlevel 1 (
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
) else (
  echo winget not available. Downloading Python 3.12 installer...
  powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe' -OutFile '%TEMP%\python_installer.exe'"
  "%TEMP%\python_installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_tcltk=1
  del "%TEMP%\python_installer.exe" >nul 2>&1
)
echo.
echo Python was just installed. Please close this window and run install.bat again.
pause
exit /b 0

:python_ok
echo Python !py_version! found.

ffmpeg -version >nul 2>&1
if not errorlevel 1 goto ffmpeg_ok

echo ffmpeg not found. Installing...
where winget >nul 2>&1
if not errorlevel 1 (
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  echo.
  echo ffmpeg was just installed. If the app fails to process audio later,
  echo close this window and run install.bat again so Windows picks up the new PATH.
  echo.
) else (
  echo winget not available. Downloading ffmpeg...
  powershell -Command "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '%TEMP%\ffmpeg.zip'"
  powershell -Command "Expand-Archive -Path '%TEMP%\ffmpeg.zip' -DestinationPath '%TEMP%\ffmpeg_extracted' -Force"
  if not exist "ffmpeg" mkdir ffmpeg
  powershell -Command "Copy-Item -Path (Get-ChildItem '%TEMP%\ffmpeg_extracted\*\bin\ffmpeg.exe').FullName -Destination 'ffmpeg\ffmpeg.exe'"
  del "%TEMP%\ffmpeg.zip" >nul 2>&1
  rmdir /s /q "%TEMP%\ffmpeg_extracted" >nul 2>&1
  set "PATH=%~dp0ffmpeg;%PATH%"
  echo ffmpeg installed to project folder.
)

:ffmpeg_ok

if exist ".venv" (
  ".venv\Scripts\python.exe" -c "import torch" >nul 2>&1
  if errorlevel 1 (
    echo Existing environment is incomplete or broken. Rebuilding...
    rmdir /s /q .venv
    goto create_venv
  )
  echo Existing environment looks healthy, skipping reinstall.
  goto model_select
)

:create_venv
echo Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
  echo Failed to create virtual environment.
  exit /b 1
)

call .venv\Scripts\activate.bat
echo Upgrading pip...
python -m pip install --upgrade pip >nul

echo.
echo Detecting hardware...
nvidia-smi >nul 2>&1
if %errorlevel% equ 0 (
  echo NVIDIA GPU detected. Installing GPU-accelerated PyTorch...
  pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
) else (
  echo No NVIDIA GPU detected. Installing CPU-only PyTorch...
  pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
)
if errorlevel 1 (
  echo PyTorch installation failed. Check your internet connection and try again.
  exit /b 1
)

echo.
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check your internet connection and try again.
  exit /b 1
)

:model_select
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
