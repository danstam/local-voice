# Local Voice

A floating desktop widget that records your speech and transcribes it locally. No internet connection required after setup. No audio ever leaves your machine.

---

## What is this?

Local Voice is a local speech-to-text tool for macOS and Windows. It was built to make writing long prompts, notes, and detailed instructions faster through dictation. No account, no API key, and no cloud processing.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Run](#run)
- [Models](#models)
- [Privacy](#privacy)

---

## Prerequisites

- macOS 14 or newer, or Windows 10/11 x64 on an Intel or AMD processor.
- Git to clone the repository.
- Internet access for the initial downloads, a working microphone, and several GB of free disk space. Larger models need more memory and storage.

You do not need to install Python, Homebrew, or manage a virtual environment. Setup handles the application's Python and dependencies separately from any existing Python installation.

Setup checks compatibility automatically and explains what to do if your computer cannot run the app.

---

## Setup

Use the same commands in **macOS Terminal** or **Windows PowerShell**:

```sh
git clone https://github.com/danstam/local-voice.git
cd local-voice
./setup.cmd
```

Setup detects your computer, prepares everything it needs, and checks that speech recognition works locally. It shows progress in the terminal and saves technical details in a log. The first installation can take a while because the downloads are large.

Setup chooses a speech model automatically, so you do not need to understand the model options to install the app. An existing Mac model preference is preserved.

If you want to choose a model yourself, this optional command installs Medium:

```sh
./setup.cmd --model medium
```

See [Models](#models) for the available choices. If Windows GPU setup fails, you can retry with `./setup.cmd --cpu`.

For an existing checkout, close the app, update the source, and rerun `./setup.cmd`. Your downloaded models and preferences are reused. If setup is interrupted, run it again to retry.

To check an existing installation without downloading or installing anything:

```sh
./setup.cmd --check
```

---

## Run

After setup, use the same command on Mac or Windows:

```sh
./start.cmd
```

The widget loads the selected model in the background and shows **Ready** when it is available. Mac starts in compact mode; Windows opens the full interface with recording controls visible and hotkeys off by default.

---

## Models

| Model | Approximate download | Notes |
| --- | --- | --- |
| `small` | 0.5 GB | Lightest option for computers with limited resources. |
| `turbo` | 1.6 GB | Everyday multilingual dictation, including English and Hebrew. |
| `medium` | 1.5 GB | Multilingual dictation and translation into English. |
| `large-v3` | 3.1 GB | Largest model; supports translation into English and needs more resources. |

The **EN** option produces English output from speech in another language. It is available with Medium and Large and runs entirely locally.

Download additional models with `./start.cmd --download-model medium`, then select the downloaded model in the widget. Replace `medium` with any model name from the table. Larger models require more memory than their download size and can be slower, especially on CPU.

---

## Privacy

All speech processing happens on your computer. There are no cloud inference calls or telemetry. Internet access is used for setup and additional downloads; dictation works offline once the model is available. Audio is processed temporarily and is not retained as recording history. Text is saved locally only when you explicitly choose to pin it.
