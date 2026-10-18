"""One serialized, restartable inference process. No GPU runtime in the UI process."""
from __future__ import annotations

import atexit
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


class TranscriptionCancelled(RuntimeError):
    pass


class InferenceClient:
    def __init__(self, command=None, timeout=None):
        if command is None:
            from .windows_runtime import command as windows_command, console_python
            command = windows_command(console_python(), "--worker")
        self.command = command or [sys.executable, "-u", "-m", "local_voice.inference_worker"]
        self.timeout = float(timeout if timeout is not None else os.environ.get("LDI_INFERENCE_TIMEOUT", 600))
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("LDI_INFERENCE_TIMEOUT must be positive")
        self._gate = threading.Lock()
        self._state = threading.Lock()
        self._process = None
        self._reader = None
        self._messages = None
        atexit.register(self.close)

    @staticmethod
    def _read_output(process, messages):
        try:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    messages.put({"kind": "error", "message": "Invalid response from inference worker"})
        finally:
            process.stdout.close()
            messages.put({"kind": "exited"})

    def _start(self):
        with self._state:
            if self._process is None:
                environment = os.environ.copy()
                # Windows pipes otherwise use the local ANSI code page. The parent
                # decodes UTF-8, and multilingual transcripts must round-trip intact.
                environment["PYTHONIOENCODING"] = "utf-8"
                # These are GPU/MLX/PyTorch subprocesses, never multiprocessing workers.
                # In particular, no multiprocessing semaphores survive a cancellation.
                process = subprocess.Popen(
                    self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    text=True, encoding="utf-8", bufsize=1,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    env=environment,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
                )
                messages = queue.Queue()
                self._process = process
                self._messages = messages
                self._reader = threading.Thread(target=self._read_output, args=(process, messages), daemon=True)
                self._reader.start()
            return self._process, self._messages

    def request(self, payload, *, cancel=None, progress_callback=None):
        while not self._gate.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
        try:
            if cancel is not None and cancel.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            process, messages = self._start()
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            last_progress = time.monotonic()
            while True:
                if cancel is not None and cancel.is_set():
                    raise TranscriptionCancelled("Transcription cancelled")
                if time.monotonic() - last_progress > self.timeout:
                    raise TimeoutError(
                        f"Inference made no decoding progress for {self.timeout:g} seconds. "
                        "The worker was stopped. Try a new recording."
                    )
                try:
                    message = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                kind = message.get("kind")
                if kind == "progress":
                    # Sent only at real loading/decoding milestones, never by a timer.
                    last_progress = time.monotonic()
                    if progress_callback is not None:
                        progress_callback(message["message"])
                elif kind == "result":
                    return message["result"]
                elif kind == "error":
                    raise RuntimeError(message["message"])
                elif kind == "exited":
                    raise RuntimeError("Inference worker exited unexpectedly. Try a new recording.")
        except BaseException:
            self.close(graceful=False)
            raise
        finally:
            self._gate.release()

    def close(self, graceful=True):
        with self._state:
            process, reader = self._process, self._reader
            self._process = self._reader = self._messages = None
        if process is not None:
            if graceful:
                # EOF lets Python/torch release runtime semaphores on a healthy exit.
                try:
                    process.stdin.close()
                    process.wait(timeout=3)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait()
            process.stdin.close()
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1)
