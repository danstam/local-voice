"""Temporary audio and pending jobs; never a recording history."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from filelock import FileLock, Timeout

PREFIX = "local-voice-session-"


class SessionWorkspace:
    def __init__(self, root=None):
        self._temporary = tempfile.TemporaryDirectory(prefix=PREFIX, dir=root)
        self.path = Path(self._temporary.name)
        (self.path / "jobs").mkdir()
        self.audio_path = self.path / "audio.f32"
        self._active = FileLock(str(self.path / ".active.lock"), thread_local=False)
        self._active.acquire(timeout=0)
        self._lock = threading.Lock()
        self.closed = False

    def job(self, index, start, end):
        target = self.path / "jobs" / f"{index:06d}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(index=index, start_frame=start, end_frame=end)), encoding="utf-8")
        temporary.replace(target)

    def close(self):
        with self._lock:
            if not self.closed:
                self._active.release()
                self._temporary.cleanup()
                self.closed = True


def cleanup_stale_workspaces(root=None):
    """Remove leftovers from a killed app, without touching live recordings."""
    root = Path(root) if root is not None else Path(tempfile.gettempdir())
    for path in root.glob(PREFIX + "*"):
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            stat = path.stat()
            if hasattr(os, "getuid") and stat.st_uid != os.getuid():
                continue
            # A new worker may still be creating/acquiring its lock.
            if time.time() - stat.st_mtime < 60 or not (path / ".active.lock").is_file():
                continue
            with FileLock(str(path / ".active.lock"), timeout=0):
                pass
            # Release the file handle first so Windows can remove the directory.
            # Names are unique; an unlocked old directory will never be reused.
            shutil.rmtree(path)
        except (Timeout, OSError):
            continue


def merge_result(transcriber, existing, result, job, previous_end):
    text = result["text"].strip()
    if job["start_frame"] < previous_end and result.get("segments"):
        text = "".join(s["text"] for s in result["segments"]
                       if job["start_frame"] + s["end"] * 16000 > previous_end).strip()
    if not text:
        return existing
    if job["start_frame"] < previous_end:
        return transcriber._merge_chunk_text(existing, text)
    return (existing + " " + text).strip()
