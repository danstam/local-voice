"""Automatic, application-owned macOS runtime setup."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import platform
import shutil
import sys
import uuid
import logging

from .windows_runtime import command
from .windows_setup import run
from .paths import app_data_dir
from . import setup_ui

PROJECT = Path(__file__).resolve().parent.parent


def active_python():
    base = PROJECT / ".mac-runtime"
    try:
        name = (base / "active-runtime.txt").read_text(encoding="utf-8").strip()
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            return None
        target = base / "runtimes" / name / "python" / "bin" / "python3"
        return target if target.is_file() else None
    except OSError:
        return None


def prepare(args):
    architecture = platform.machine()
    lock = PROJECT / "macos" / f"requirements-{architecture}.lock"
    digest = hashlib.sha256(lock.read_bytes() + (PROJECT / "macos" / "runtime.json").read_bytes()).hexdigest()
    current = active_python()
    if current:
        try:
            if (current.parent.parent / "runtime-fingerprint.txt").read_text().strip() == digest:
                run(command(current, "--mac-setup", "--probe"), timeout=120)
                return current
        except (OSError, RuntimeError):
            logging.warning("Preparing a replacement runtime; the previous copy will be preserved.")
    if args.bootstrap is None:
        raise RuntimeError("Run ./setup.cmd to prepare the private macOS runtime.")
    base = PROJECT / ".mac-runtime"
    stage = base / "runtimes" / ("runtime-" + uuid.uuid4().hex)
    try:
        if shutil.disk_usage(PROJECT).free < 4 * 1024**3:
            raise RuntimeError("Free at least 4 GiB on the project drive, then retry.")
        shutil.copytree(args.bootstrap / "python", stage / "python")
        python = stage / "python" / "bin" / "python3"
        prefix = [str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install",
                  "--disable-pip-version-check", "--no-cache-dir", "--index-url", "https://pypi.org/simple"]
        run(prefix + ["--require-hashes", "-r", str(PROJECT / "windows" / "build.lock")])
        run(prefix + ["--require-hashes", "--no-build-isolation", "--only-binary=:all:",
                      "--no-binary=openai-whisper", "-r", str(lock)])
        run([str(python), "-I", "-m", "pip", "--isolated", "check"])
        run(command(python, "--mac-setup", "--probe"), timeout=120)
        (stage / "python" / "runtime-fingerprint.txt").write_text(digest, encoding="utf-8")
        temporary = base / ("active-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(stage.name, encoding="utf-8")
        temporary.replace(base / "active-runtime.txt")
        return python
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def setup(args):
    setup_ui.say("Checking your computer...")
    from .transcriber import WhisperTranscriber
    if not args.check:
        setup_ui.say("Preparing Local Voice... this can take a few minutes on the first run.")
    python = active_python() if args.check else prepare(args)
    if python is None:
        raise RuntimeError("No private runtime found. Run ./setup.cmd first; no downloads were attempted.")
    if args.prepare_only:
        setup_ui.say("Local Voice is prepared. Run ./setup.cmd to download and test the speech model.")
        return
    # Preserve an existing model preference when moving from the legacy environment.
    transcriber = WhisperTranscriber()
    model = args.model or transcriber.current_model_name()
    if not args.check:
        setup_ui.say("Preparing the speech model... we'll use a suitable option for this computer.")
        run(command(python, "--maintenance", "--download-model", model))
    setup_ui.say("Testing the speech engine...")
    run(command(python, "--mac-setup", "--verify-model", model,
                *(["--check"] if args.check else []), *(["--cpu"] if args.cpu else [])))
    setup_ui.say("Checks passed." if args.check else "Setup complete.")
    setup_ui.say("Run ./start.cmd to open Local Voice.")


def main():
    parser = argparse.ArgumentParser(description="Prepare Local Voice automatically for this Mac.")
    parser.add_argument("--bootstrap", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="Check an existing installation offline")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prepare-only", action="store_true", help="Prepare the app without downloading a speech model")
    parser.add_argument("--verify-model", help=argparse.SUPPRESS)
    parser.add_argument("--model", choices=["small", "turbo", "medium", "large-v3"], help="Advanced: choose a specific speech model")
    parser.add_argument("--cpu", action="store_true", help="Test speech recognition using the processor")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This runtime setup is for macOS.")
    if args.probe:
        for name in ("AppKit", "Quartz", "sounddevice", "soundfile", "webrtcvad", "torch"):
            importlib.import_module(name)
        if platform.machine() == "arm64":
            importlib.import_module("mlx_whisper")
        print("Private macOS dependencies loaded successfully.")
        return
    from .transcriber import WhisperTranscriber
    if args.verify_model:
        import os
        if args.cpu or platform.machine() == "x86_64":
            os.environ["LDI_DEVICE"] = "cpu"
        transcriber = WhisperTranscriber()
        try:
            transcriber.set_model_name(args.verify_model)
            transcriber._load_model(progress_callback=print)
            if not args.check:
                transcriber.save_model_preference(args.verify_model)
        finally:
            transcriber.unload_model()
        return
    target = app_data_dir() / "mac-setup.log"
    with setup_ui.SetupConsole(target) as console:
        logging.info("Starting macOS %s", "checks" if args.check else "setup")
        try:
            setup(args)
        except (Exception, KeyboardInterrupt) as exc:
            console.failed(exc)
            raise SystemExit(1)
