"""Plain terminal progress with technical setup output kept in a local log."""
from contextlib import contextmanager
import logging
import re
import threading
import time

_active = None


def say(message):
    if _active is not None:
        _active.message(message)


def progress(line):
    if _active is not None:
        _active.download_progress(line)


@contextmanager
def waiting():
    if _active is None:
        yield
    else:
        with _active.waiting():
            yield


class SetupConsole:
    def __init__(self, path):
        self.path = path
        self.last_activity = time.monotonic()
        self.last_percent = -5
        self._lock = threading.Lock()

    def __enter__(self):
        global _active
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.path, mode="a", encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger = logging.getLogger()
        self.previous_level = self.logger.level
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.previous_console = _active
        _active = self
        return self

    def __exit__(self, *_):
        global _active
        _active = self.previous_console
        self.logger.removeHandler(self.handler)
        self.handler.close()
        self.logger.setLevel(self.previous_level)

    def message(self, message):
        with self._lock:
            print(message, flush=True)
            self.last_activity = time.monotonic()

    def download_progress(self, line):
        match = re.fullmatch(r"Downloaded ([\d.]+) of ([\d.]+) MB \((\d+)%\)", line.strip())
        if match:
            percent = int(match[3])
            if percent >= self.last_percent + 5 or (percent == 100 and self.last_percent != 100):
                self.last_percent = percent
                self.message(f"Downloading the speech model... {percent}% ({match[1]} of {match[2]} MB)")

    @contextmanager
    def waiting(self):
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(30):
                if time.monotonic() - self.last_activity >= 30:
                    self.message("Still working... large downloads and first-time checks can take a few minutes.")

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)

    def failed(self, error):
        logging.getLogger(__name__).error("Setup stopped", exc_info=True)
        detail = str(error).lower()
        if isinstance(error, KeyboardInterrupt):
            message = "Setup cancelled. Run ./setup.cmd again when you're ready."
        elif "no private runtime" in detail or "not cached" in detail:
            message = "Setup is not complete yet. Run ./setup.cmd first."
        elif "disk" in detail or "free space" in detail or "free at least" in detail or "no space left" in detail:
            message = "There isn't enough free disk space. Free some space, then run ./setup.cmd again."
        elif "available ram" in detail or "out of memory" in detail:
            message = "There isn't enough available memory. Close other apps, or retry with ./setup.cmd --model small."
        elif "microphone" in detail or "portaudio" in detail or "input device" in detail:
            message = "The microphone check failed. Connect a microphone and check microphone permissions, then retry."
        elif "visual c++" in detail or "dll" in detail:
            message = "A Windows component could not load. Check the log for the missing component and repair instructions."
        elif "certificate" in detail or "download" in detail or "connection" in detail or "network" in detail:
            message = "A download could not finish. Check your internet connection, then run ./setup.cmd again."
        elif isinstance(error, PermissionError):
            message = "Local Voice couldn't write its files. Move the project to a folder you can edit, then retry."
        else:
            message = "Setup could not finish. Run ./setup.cmd again; if it still fails, use the log below to diagnose it."
        self.message(message)
        self.message(f"Details saved to: {self.path}")
