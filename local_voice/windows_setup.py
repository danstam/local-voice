"""Windows setup and offline diagnostics; bootstrap needs only Python's standard library."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import uuid
from collections import deque

from .paths import app_data_dir
from .windows_runtime import active_python, activate_runtime, command
from . import setup_ui

PROJECT = Path(__file__).resolve().parent.parent
GIB = 1024 ** 3
# Published Python 3.12 Windows wheels, including Blackwell support on cu128.
TORCH_VERSION = "2.9.1"
# Planning estimates, not guarantees. The real decoding warmup is the final check.
MODELS = {
    "small": dict(download=0.5, gpu=2, cpu=4),
    "turbo": dict(download=1.6, gpu=6, cpu=8),
    "medium": dict(download=1.5, gpu=5, cpu=8),
    "large-v3": dict(download=3.1, gpu=10, cpu=14),
}
log = logging.getLogger("local_voice.setup")


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in (
            "total_physical", "available_physical", "total_pagefile", "available_pagefile",
            "total_virtual", "available_virtual", "available_extended_virtual",
        )
    ]


def system_memory():
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    api.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not api.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"total_gib": status.total_physical / GIB, "available_gib": status.available_physical / GIB}


def torch_index(gpus):
    """nvidia-smi chooses a wheel to try; it does not establish working CUDA."""
    if not gpus:
        return "cpu"
    # CUDA 12.8 wheels drop older architectures; 12.6 covers Maxwell/Pascal/Volta.
    return "cu128" if gpus[0]["capability"] >= 7.5 else "cu126"


def nvidia_gpus():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            log.warning("NVIDIA query failed; using CPU setup. %s", result.stderr.strip())
            return []
        rows = []
        for line in result.stdout.splitlines():
            name, memory, capability = (part.strip() for part in line.rsplit(",", 2))
            rows.append(dict(name=name, memory_gib=float(memory) / 1024, capability=float(capability)))
        return rows
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        log.info("No usable NVIDIA hardware report; using CPU setup: %s", exc)
        return []


def run(command, *, capture=False, timeout=None):
    log.info("Running: %s", subprocess.list2cmdline([str(part) for part in command]))
    environment = os.environ.copy()
    for key in ("LDI_MODEL", "LDI_DEVICE", "LDI_BACKEND", "LDI_LANGUAGE"):
        environment.pop(key, None)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    with setup_ui.waiting(), subprocess.Popen(command, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=environment,
                          **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {})) as process:
        lines = []
        recent = deque(maxlen=20)
        if timeout is not None:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
                log.error(output.rstrip())
                raise RuntimeError(f"Compatibility check did not finish within {timeout} seconds; the check was stopped.") from None
            for line in output.splitlines():
                log.info(line)
                recent.append(line)
            if capture:
                lines.append(output)
        else:
            for line in process.stdout:
                log.info(line.rstrip())
                recent.append(line.rstrip())
                setup_ui.progress(line)
                if capture:
                    lines.append(line)
        code = process.wait()
        if code:
            raise RuntimeError(f"This step failed with exit code {code}.\n" + "\n".join(recent))
    return "".join(lines)


def probe_runtime(*, force_cpu=False):
    """Run in the installed environment, independently of the bootstrap interpreter."""
    try:
        import torch
    except OSError as exc:
        raise RuntimeError("PyTorch could not load its native DLLs. Check Microsoft's Visual C++ x64 Redistributable and the error details: " + str(exc)) from exc
    for name in ("whisper", "sounddevice", "soundfile", "webrtcvad", "filelock", "tkinter"):
        importlib.import_module(name)
    # Importing tkinter does not prove Tcl/Tk was installed correctly.
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    root.destroy()
    torch.ones((2, 2)).matmul(torch.ones((2, 2)))
    result = dict(device="cpu", torch=torch.__version__, gpu=None, gpu_free_gib=0)
    if not force_cpu and torch.cuda.is_available():
        try:
            sample = torch.ones((32, 32), device="cuda", dtype=torch.float16)
            sample.matmul(sample)
            torch.cuda.synchronize()
            free, _total = torch.cuda.mem_get_info()
            result.update(device="cuda", gpu=torch.cuda.get_device_name(0), gpu_free_gib=free / GIB)
            del sample
            torch.cuda.empty_cache()
        except Exception as exc:
            result.update(device="cpu", gpu=None, gpu_free_gib=0)
            result["cuda_error"] = str(exc)
    elif not force_cpu and torch.version.cuda:
        result["cuda_error"] = "CUDA unavailable in this PyTorch/driver combination. CPU computation passed."
    return result


def memory_warning(model, runtime, memory):
    warnings = []
    if runtime["device"] == "cuda":
        needed = MODELS[model]["gpu"] + 1
        if runtime["gpu_free_gib"] < needed:
            warnings.append(f"about {needed:g} GiB free GPU memory recommended; detected {runtime['gpu_free_gib']:.1f} GiB")
    # The worker loads checkpoints on CPU first even when inference uses CUDA.
    needed = MODELS[model]["cpu"]
    if memory["available_gib"] < needed:
        warnings.append(f"about {needed:g} GiB available RAM recommended; detected {memory['available_gib']:.1f} GiB")
    return f"{model}: {'; '.join(warnings)}." if warnings else None


def choose_model(runtime, memory, requested=None):
    recommended = "turbo" if runtime["device"] == "cuda" and not memory_warning("turbo", runtime, memory) else "small"
    if runtime["device"] == "cpu":
        log.info("CPU inference is available, but can be substantially slower than real time.")
    if requested is None:
        log.info("Models (download size): 1) Small ~0.5 GB  2) Turbo ~1.6 GB  3) Medium ~1.5 GB  4) Large ~3.1 GB")
        log.info("Recommended starting model for this computer: %s", recommended)
        log.info("Small uses fewer resources and may be less accurate. English translation requires Medium or Large.")
        for model in MODELS:
            warning = memory_warning(model, runtime, memory)
            if warning:
                log.warning(warning)
        selection = input(f"Model [1-4 or name; Enter for {recommended}]: ").strip()
        requested = dict(zip(("1", "2", "3", "4"), MODELS)).get(selection, selection or recommended)
    if requested not in MODELS:
        raise ValueError("Choose a model from the list, then rerun setup.")
    warning = memory_warning(requested, runtime, memory)
    if warning:
        log.warning(warning)
        if input("This model may not fit. Try it anyway? [y/N]: ").strip().lower() != "y":
            raise RuntimeError("Choose a smaller model or close other apps, then rerun setup.")
    return requested


def verify_model(model, device, *, save=False):
    # Use the SAME worker and decoding warmup as the application.
    os.environ["LDI_BACKEND"] = "torch"
    os.environ["LDI_DEVICE"] = device
    from .transcriber import WhisperTranscriber
    transcriber = WhisperTranscriber()
    try:
        transcriber.set_model_name(model)
        transcriber._load_model(progress_callback=print)
        if save:
            transcriber._write_settings({"model": model, "windows_device": device})
    finally:
        transcriber.unload_model()


def check_microphone():
    import sounddevice as sd
    from .audio_devices import resolve_input_device
    from .settings import read_settings
    index = resolve_input_device(read_settings().get("windows_input_device"))
    device = sd.query_devices(index, "input")
    try:
        sd.check_input_settings(device=index, channels=1, dtype="float32", samplerate=16000)
        rate = 16000
    except sd.PortAudioError:
        rate = device["default_samplerate"]
        sd.check_input_settings(device=index, channels=1, dtype="float32", samplerate=rate)
        import soxr
    log.info("Microphone: %s at %s Hz; native-rate audio is resampled locally when needed. No audio recorded.", device["name"], rate)


def validate_system():
    if sys.getwindowsversion().major < 10:
        raise RuntimeError("Windows 10 or 11 is required for this installer.")
    if struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("This Windows installer supports x64 PCs. Windows ARM/32-bit is not validated.")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Run ./setup.cmd to prepare the supported application runtime.")
    memory = system_memory()
    log.info("Windows %s; CPU: %s; Python: %s", platform.win32_ver()[0], platform.processor(), sys.executable)
    log.info("RAM: %.1f GiB total, %.1f GiB available", memory["total_gib"], memory["available_gib"])
    data = app_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    log.info("Free space: project %.1f GiB; models %.1f GiB", shutil.disk_usage(PROJECT).free / GIB,
             shutil.disk_usage(data).free / GIB)
    return memory, data


def runtime_fingerprint(variant):
    import hashlib
    digest = hashlib.sha256()
    for name in ("runtime.json", "requirements.lock", "build.lock", f"torch-{variant}.lock"):
        digest.update((PROJECT / "windows" / name).read_bytes())
    return digest.hexdigest()


def probe(python, *, cpu=False):
    report = run(command(python, "--setup", "--probe", *(["--cpu"] if cpu else [])), capture=True, timeout=120)
    return json.loads(report.strip().splitlines()[-1])


def prepare_runtime(args):
    from hashlib import sha256
    index = "cpu" if args.cpu else torch_index(nvidia_gpus())
    fingerprint = runtime_fingerprint(index)
    current = active_python(PROJECT)
    if current is not None:
        try:
            ready = json.loads((current.parent.parent / "runtime-ready.json").read_text(encoding="utf-8"))
            if ready.get("fingerprint") == fingerprint:
                return current, probe(current, cpu=args.cpu)
        except (OSError, ValueError, RuntimeError):
            log.warning("The previous private runtime needs repair; it will be preserved until a replacement passes.")
    bootstrap = getattr(args, "bootstrap", None)
    if bootstrap is None or not (bootstrap / "python" / "python.exe").is_file():
        raise RuntimeError("Use ./setup.cmd to prepare the app-owned Python runtime.")
    private = PROJECT / ".windows"
    archive = private / "python-runtime.tar.gz"
    manifest = json.loads((PROJECT / "windows" / "runtime.json").read_text(encoding="utf-8"))
    with archive.open("rb") as handle:
        if sha256(handle.read()).hexdigest() != manifest["sha256"]:
            raise RuntimeError("Private Python checksum mismatch. Rerun setup.")
    variants = [index] + (["cpu"] if index != "cpu" else [])
    for variant in variants:
        if shutil.disk_usage(PROJECT).free < (8 if variant != "cpu" else 4) * GIB:
            if variant != "cpu":
                log.warning("Insufficient disk space for GPU packages; trying the smaller CPU runtime.")
                continue
            raise RuntimeError("Free 8 GiB for GPU setup or 4 GiB for CPU setup, then retry.")
        stage = private / ("staging-" + uuid.uuid4().hex)
        final = None
        try:
            shutil.copytree(bootstrap / "python", stage / "python")
            python = stage / "python" / "python.exe"
            prefix = [str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-cache-dir", "--index-url", "https://pypi.org/simple"]
            run(prefix + ["--require-hashes", "-r", str(PROJECT / "windows" / "build.lock")])
            run(prefix + ["--require-hashes", "--no-build-isolation", "--only-binary=:all:",
                          "--no-binary=openai-whisper", "-r", str(PROJECT / "windows" / "requirements.lock"),
                          "-r", str(PROJECT / "windows" / f"torch-{variant}.lock")])
            run([str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "check"])
            runtime = probe(python, cpu=variant == "cpu")
            (stage / "runtime-ready.json").write_text(json.dumps(dict(
                fingerprint=runtime_fingerprint(variant), variant=variant, runtime=runtime)), encoding="utf-8")
            final = private / "runtimes" / ("runtime-" + uuid.uuid4().hex)
            final.parent.mkdir(parents=True, exist_ok=True)
            stage.replace(final)
            python = final / "python" / "python.exe"
            # Recheck after relocation before switching the active pointer.
            runtime = probe(python, cpu=variant == "cpu")
            run(command(python, "--setup", "--ui-check"), timeout=60)
            activate_runtime(python, PROJECT)
            return python, runtime
        except (Exception, KeyboardInterrupt):
            if variant == "cpu" or isinstance(sys.exc_info()[1], KeyboardInterrupt):
                raise
            log.exception("GPU installation/check failed. Retrying in a fresh CPU-only runtime.")
            setup_ui.say("Preparing a compatible speech engine using your computer's processor...")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if final is not None and active_python(PROJECT) != final / "python" / "python.exe":
                shutil.rmtree(final, ignore_errors=True)
    raise RuntimeError("No compatible runtime could be prepared.")


def setup(args):
    setup_ui.say("Checking your computer...")
    memory, data = validate_system()
    if args.check:
        python = active_python(PROJECT)
        if python is None:
            raise RuntimeError("No private runtime found. Run ./setup.cmd first. No downloads were attempted.")
        runtime = probe(python, cpu=args.cpu)
    else:
        setup_ui.say("Preparing Local Voice... this can take a few minutes on the first run.")
        python, runtime = prepare_runtime(args)
    log.info("Verified backend: PyTorch %s on %s (%s)", runtime["torch"], runtime["device"], runtime["gpu"] or "CPU")
    if runtime.get("cuda_error"):
        log.warning("GPU unavailable; using CPU: %s", runtime["cuda_error"])
        setup_ui.say("Using your computer's processor for speech recognition.")
    log.info("Checking the Windows interface at 100%%, 150%%, and 200%% display scaling…")
    setup_ui.say("Checking the interface...")
    run(command(python, "--setup", "--ui-check"), timeout=60)
    if getattr(args, "prepare_only", False):
        setup_ui.say("Local Voice is prepared. Run ./start.cmd to finish setting up your speech model.")
        return
    if args.check:
        setup_ui.say("Testing the speech engine...")
        from .settings import read_settings
        saved = read_settings()
        model = args.model or saved.get("model", "small")
        device = "cpu" if args.cpu or saved.get("windows_device") == "cpu" else runtime["device"]
        run(command(python, "--setup", "--verify-model", model, "--device", device))
    else:
        model = args.model
        if model is None:
            model = "turbo" if runtime["device"] == "cuda" and not memory_warning("turbo", runtime, memory) else "small"
        warning = memory_warning(model, runtime, memory)
        if warning:
            raise RuntimeError(warning + " Choose a smaller model or close other apps, then retry.")
        if shutil.disk_usage(data).free < (MODELS[model]["download"] * 2 + 1) * GIB:
            raise RuntimeError("Not enough free space for the selected model.")
        log.info("Selected model: %s. Downloading and verifying locally…", model)
        setup_ui.say("Preparing the speech model... we'll use a suitable option for this computer.")
        run(command(python, "--maintenance", "--download-model", model))
        setup_ui.say("Testing the speech engine...")
        try:
            run(command(python, "--setup", "--verify-model", model, "--device", runtime["device"], "--save"))
        except RuntimeError:
            if runtime["device"] != "cuda" or memory["available_gib"] < MODELS[model]["cpu"]:
                raise
            log.warning("GPU model warmup failed. Trying the same model on CPU.")
            setup_ui.say("Trying speech recognition with your computer's processor...")
            run(command(python, "--setup", "--verify-model", model, "--device", "cpu", "--save"))
    setup_ui.say("Checking the microphone...")
    run(command(python, "--setup", "--microphone"), timeout=60)
    log.info("%s verified. Run ./start.cmd to launch.", "Diagnostics" if args.check else "Setup")
    from .command_install import finish_setup
    finish_setup(PROJECT, check=args.check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--prepare-only", action="store_true", help="Prepare the private runtime without downloading a model")
    parser.add_argument("--check", action="store_true", help="Offline diagnostics; no installs or downloads")
    parser.add_argument("--remove-command", action="store_true", help="Remove the voice shortcut without removing Local Voice")
    parser.add_argument("--cpu", action="store_true", help="Use CPU even if an NVIDIA GPU is present")
    parser.add_argument("--model", choices=list(MODELS))
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--microphone", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verify-model", choices=list(MODELS), help=argparse.SUPPRESS)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help=argparse.SUPPRESS)
    parser.add_argument("--save", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check and args.remove_command:
        parser.error("Choose either --check or --remove-command; checks do not change shortcuts.")
    if sys.platform != "win32":
        parser.exit(1, "Run ./setup.cmd to choose the right setup for this computer.\n")
    if args.probe:
        print(json.dumps(probe_runtime(force_cpu=args.cpu)))
        return
    if args.ui_check:
        from .windows_ui_check import check_ui
        print(json.dumps(check_ui()))
        return
    if args.verify_model:
        verify_model(args.verify_model, args.device, save=args.save)
        return
    if args.microphone:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        check_microphone()
        return
    folder = app_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "windows-setup.log"
    with setup_ui.SetupConsole(target) as console:
        log.info("\n=== Local Voice Windows %s (%s) ===", "diagnostics" if args.check else "setup",
                 datetime.now().isoformat(timespec="seconds"))
        try:
            if args.remove_command:
                from .command_install import remove_with_output
                remove_with_output(PROJECT)
            else:
                setup(args)
        except (Exception, KeyboardInterrupt) as exc:
            console.failed(exc)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
