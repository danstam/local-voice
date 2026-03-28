# Local Voice

A floating desktop widget that records and transcribes your speech locally. No cloud. No account. After setup and model download, normal use is fully offline and nothing leaves your device.

It can also translate spoken non-English audio into English text when `medium` or `large-v3` is selected.

---

## Setup

**macOS**
```bash
git clone https://github.com/danstam/local-voice.git
cd local-voice
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/danstam/local-voice.git
cd local-voice
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
winget install Gyan.FFmpeg
```

---

## Download a model (required once)

Before first use, download at least one Whisper model. After that, transcription runs offline.

**macOS**
```bash
./voice --download-model <model-name>
```

**Windows**
```powershell
.\voice_windows.bat --download-model <model-name>
```

| Model | Size | Recommended for |
|---|---|---|
| `small` | ~461 MB | Smallest multilingual option |
| `turbo` | ~1.6 GB | Fastest general transcription, but not for `EN` translation |
| `medium` | ~1.4 GB | Non-English speakers — supports translation to English |
| `large-v3` | ~3.1 GB | Highest accuracy, but heavy on most hardware |

Replace `<model-name>` with one of: `small`, `turbo`, `medium`, or `large-v3`.

If you're only transcribing in English, start with `turbo`. If you plan to speak in another language and get English output, use `medium`.

---

## Run

**macOS**
```bash
./voice
```

**Windows**
```powershell
.\voice_windows.bat
```

---

## Translate to English

Toggle the **EN** button to output English text from spoken non-English audio. The toggle is available only when `medium` or `large-v3` is selected.
