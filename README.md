# Local Voice

A floating desktop widget that records your speech and transcribes it locally. No internet connection required after setup. No audio ever leaves your machine.

---

## What is this?

Local Voice is a local speech-to-text tool that runs entirely on your machine. It was built to speed up the process of writing long, detailed prompts for AI agents — dictating an architecture decision or a complex instruction is significantly faster than typing it. The result lands in the widget's text area, ready to copy anywhere. No account, no API key, and no audio ever leaves your machine.

The widget runs as a floating panel that stays on top of other windows. On macOS it uses native Cocoa. On Windows it uses tkinter. Both implementations share the same recording and transcription backend.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Run](#run)
- [Models](#models)
- [Architecture](#architecture)

---

## Prerequisites

- Python 3.10 or higher
- Git
- ffmpeg — macOS only: `brew install ffmpeg` (Windows installer handles this automatically)

---

## Setup

Clone the repository and run the installer. The installer will set up the environment, install dependencies, and walk you through downloading a model.

**macOS**

```bash
git clone https://github.com/danstam/local-voice.git
cd local-voice
./install.sh
```

**Windows**

```bat
git clone https://github.com/danstam/local-voice.git
cd local-voice
install.bat
```

The installer will prompt you to select a model before finishing. See [Models](#models) for guidance on which one to pick. Dependencies include PyTorch (~2 GB) and will be downloaded once during this step.

---

## Run

**macOS**

```bash
./voice
```

**Windows**

```bat
voice_windows.bat
```

The widget appears on the right side of the screen. On first launch it loads the model in the background — the status indicator shows **Loading** until it is ready. Subsequent launches are faster once the model is warm in memory.

---

## Models

| Model | Size | Speed | Notes |
|---|---|---|---|
| `small` | ~461 MB | Fast | Good for clear English. Can be imprecise. |
| `turbo` | ~1.6 GB | Fast | Best quality-to-speed ratio for English. |
| `medium` | ~1.4 GB | Moderate | Best balance for non-English speakers. Supports the EN toggle. |
| `large-v3` | ~3.1 GB | Slow | Highest accuracy across all languages. Supports the EN toggle. Heavy for most hardware. |

The **EN** button transcribes directly into English regardless of what language you speak — no separate translation step. It is only available when `medium` or `large-v3` is active.

The model can be switched at any time from the dropdown in the widget header. Switching loads the new model in the background without restarting the app. Only models that have been downloaded are available for selection.

---

## Architecture

**Chunked transcription**

Recording and transcription happen concurrently. While you are speaking, the engine continuously sends audio chunks for processing in the background — it does not wait for you to stop. By the time you press Stop, most of the audio has already been processed. Only the final chunk remains.

This means transcription latency is roughly constant regardless of recording length. A 20-minute session and a 10-second session feel the same at the end. Chunk boundaries are handled by a word-overlap merge that detects and removes duplicate words at the seam between consecutive chunks, so the output reads as a single continuous transcript.

**Hardware acceleration**

The engine detects the best available device automatically — Apple Silicon (MPS), NVIDIA GPU (CUDA), or CPU — and loads the model accordingly. No configuration needed.

### Privacy

All processing happens on your machine. There are no API calls, no telemetry, and no network requests during normal use. The only outbound connection is the one-time model download.