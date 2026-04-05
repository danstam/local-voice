#!/bin/zsh
set -euo pipefail

echo ""
echo "  \033[34mWelcome to Local Voice\033[0m"
echo "  ─────────────────────────"
echo "  Speech to text, fully offline."
echo ""
echo "  This installer will:"
echo "    • Install Python 3.12 if needed"
echo "    • Install ffmpeg if needed"
echo "    • Install dependencies (includes PyTorch, ~2 GB)"
echo "    • Download a Whisper transcription model"
echo ""
printf "  Press Enter to continue..."
read -r _
echo ""

if ! command -v python3 &>/dev/null; then
  if command -v brew &>/dev/null; then
    echo "Python not found. Installing via Homebrew..."
    brew install python@3.12
    export PATH="$(brew --prefix python@3.12)/bin:$PATH"
  else
    echo "Python 3.10 or higher is required."
    echo "Install Homebrew first (https://brew.sh), then run: brew install python@3.12"
    exit 1
  fi
fi

py_version=$(python3 -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)")
if (( py_version < 310 )); then
  if command -v brew &>/dev/null; then
    echo "Python $(python3 --version) is too old. Installing Python 3.12 via Homebrew..."
    brew install python@3.12
    export PATH="$(brew --prefix python@3.12)/bin:$PATH"
  else
    echo "Python 3.10 or higher is required. You have $(python3 --version)."
    echo "Install Homebrew (https://brew.sh) then run: brew install python@3.12"
    exit 1
  fi
fi

echo "Python $(python3 --version) found."

if ! command -v ffmpeg &>/dev/null; then
  if command -v brew &>/dev/null; then
    echo "Installing ffmpeg via Homebrew..."
    brew install ffmpeg
  else
    echo "ffmpeg is required but not installed."
    echo "Install Homebrew first (https://brew.sh), then run: brew install ffmpeg"
    exit 1
  fi
fi

if [[ -d ".venv" ]]; then
  if ! .venv/bin/python -c "import torch" &>/dev/null; then
    echo "Existing environment is incomplete or broken. Rebuilding..."
    rm -rf .venv
  else
    echo "Existing environment looks healthy, skipping reinstall."
    goto_model_select=1
  fi
fi

if [[ -z "${goto_model_select:-}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv

  echo "Installing dependencies..."
  .venv/bin/pip install --upgrade pip --quiet
  .venv/bin/pip install -r requirements.txt
fi

chmod +x voice

echo ""
echo "Select a model to download:"
echo ""
echo "  1) Small   (~461 MB)   — fast, good for clear English"
echo "  2) Turbo   (~1.6 GB)   — best quality/speed ratio for English"
echo "  3) Medium  (~1.4 GB)   — best for non-English languages, supports translation"
echo "  4) Large   (~3.1 GB)   — highest accuracy, slow on most hardware"
echo ""
printf "Enter a number [1-4]: "
read selection

case "$selection" in
  1) model="small" ;;
  2) model="turbo" ;;
  3) model="medium" ;;
  4) model="large-v3" ;;
  *)
    echo "Invalid selection."
    exit 1
    ;;
esac

echo ""
echo "Downloading model: $model"
if .venv/bin/python -m local_voice.app_mac --download-model "$model"; then
  echo ""
  echo "Setup complete. Run ./voice to launch."
else
  exit_code=$?
  echo "Model download failed. Check your internet connection and try again."
  exit $exit_code
fi
