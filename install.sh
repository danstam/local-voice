#!/bin/zsh
set -euo pipefail

echo ""
echo "  \033[34mWelcome to Local Voice\033[0m"
echo "  ─────────────────────────"
echo "  Speech to text, fully offline."
echo ""
echo "  This installer will:"
echo "    • Create a Python virtual environment"
echo "    • Install dependencies (includes PyTorch, ~2 GB)"
echo "    • Download a Whisper transcription model"
echo ""
printf "  Press Enter to continue..."
read -r _
echo ""

# Check Python 3.10+
if ! command -v python3 &>/dev/null; then
  echo "Python 3 is not installed. Please install Python 3.10 or higher from https://www.python.org and try again."
  exit 1
fi

py_version=$(python3 -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)")
if (( py_version < 310 )); then
  echo "Python 3.10 or higher is required. You have $(python3 --version). Please upgrade and try again."
  exit 1
fi

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "ffmpeg is not installed."
  echo "If you have Homebrew, run: brew install ffmpeg"
  echo "If you don't have Homebrew, install it first from https://brew.sh, then run: brew install ffmpeg"
  exit 1
fi

# Create virtual environment
if [[ -d ".venv" ]]; then
  echo "Virtual environment already exists, skipping."
else
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# Install requirements
echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt

# Make launcher executable
chmod +x voice

# Model selection menu
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
