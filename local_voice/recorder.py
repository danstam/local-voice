from __future__ import annotations

from collections import deque
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
CHANNELS = 1


class Recorder:
    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._chunks: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._base_frame = 0
        self._total_frames = 0
        self._paused = False
        self._level = 0.0

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("Already recording")

            self._chunks = deque()
            self._base_frame = 0
            self._total_frames = 0
            self._level = 0.0

            def callback(indata: np.ndarray, frames: int, time_info, status) -> None:
                if status:
                    print(f"recording status: {status}")
                chunk = indata.copy()
                level = self._normalize_level(chunk)
                with self._lock:
                    self._chunks.append(chunk)
                    self._total_frames += len(chunk)
                    if level >= self._level:
                        self._level = (self._level * 0.68) + (level * 0.32)
                    else:
                        self._level = (self._level * 0.86) + (level * 0.14)

            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=callback,
            )
            self._stream = stream
            self._paused = False

        try:
            stream.start()
        except Exception:
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                    self._paused = False
                    self._level = 0.0
            stream.close()
            raise

    def pause(self) -> None:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("Not recording")
            if self._paused:
                raise RuntimeError("Already paused")
            stream = self._stream

        stream.stop()

        with self._lock:
            if self._stream is stream:
                self._paused = True
                self._level = 0.0

    def resume(self) -> None:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("Not recording")
            if not self._paused:
                raise RuntimeError("Not paused")
            stream = self._stream

        stream.start()

        with self._lock:
            if self._stream is stream:
                self._paused = False

    def stop(self) -> None:
        with self._lock:
            if self._stream is None:
                raise RuntimeError("Not recording")
            stream = self._stream
            was_paused = self._paused
            self._stream = None
            self._paused = False
            self._level = 0.0

        try:
            if not was_paused:
                stream.stop()
        finally:
            stream.close()

    def total_frames(self) -> int:
        with self._lock:
            return self._total_frames

    def current_level(self) -> float:
        with self._lock:
            return self._level

    def copy_range(self, start_frame: int, end_frame: int) -> np.ndarray:
        with self._lock:
            start_frame = max(start_frame, self._base_frame)
            end_frame = min(end_frame, self._total_frames)
            if end_frame <= start_frame:
                return np.zeros((0, CHANNELS), dtype=np.float32)

            current_frame = self._base_frame
            parts: list[np.ndarray] = []
            for chunk in self._chunks:
                chunk_end = current_frame + len(chunk)
                if chunk_end <= start_frame:
                    current_frame = chunk_end
                    continue
                if current_frame >= end_frame:
                    break

                local_start = max(0, start_frame - current_frame)
                local_end = min(len(chunk), end_frame - current_frame)
                if local_end > local_start:
                    parts.append(chunk[local_start:local_end].copy())
                current_frame = chunk_end

            if not parts:
                return np.zeros((0, CHANNELS), dtype=np.float32)
            return np.concatenate(parts, axis=0)

    def drop_before(self, frame_index: int) -> None:
        with self._lock:
            target = max(self._base_frame, min(frame_index, self._total_frames))
            while self._chunks and self._base_frame + len(self._chunks[0]) <= target:
                first = self._chunks.popleft()
                self._base_frame += len(first)

            if self._chunks and target > self._base_frame:
                trim = target - self._base_frame
                self._chunks[0] = self._chunks[0][trim:].copy()
                self._base_frame = target

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._base_frame = 0
            self._total_frames = 0
            self._paused = False
            self._level = 0.0

    def _normalize_level(self, chunk: np.ndarray) -> float:
        if chunk.size == 0:
            return 0.0

        rms = float(np.sqrt(np.mean(np.square(chunk[:, 0]))))
        return min(1.0, rms * 16.0)
