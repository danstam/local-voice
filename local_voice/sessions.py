from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from .recorder import Recorder, SAMPLE_RATE
from .transcriber import WhisperTranscriber

LONG_RECORDING_CHUNK_SECONDS = 60
LONG_RECORDING_OVERLAP_SECONDS = 6
FINALIZE_POLL_INTERVAL_SECONDS = 0.5
FINALIZE_INACTIVITY_TIMEOUT_SECONDS = 180


@dataclass
class ChunkJob:
    index: int
    start_frame: int
    end_frame: int
    audio: np.ndarray


class ChunkedRecordingSession:
    def __init__(self, recorder: Recorder, transcriber: WhisperTranscriber) -> None:
        self.recorder = recorder
        self.transcriber = transcriber
        self.model_name = transcriber.current_model_name()
        self.device = transcriber._default_device()
        self.chunk_frames = LONG_RECORDING_CHUNK_SECONDS * SAMPLE_RATE
        self.overlap_frames = LONG_RECORDING_OVERLAP_SECONDS * SAMPLE_RATE
        self.step_frames = self.chunk_frames - self.overlap_frames
        self.chunk_queue: queue.Queue[ChunkJob | None] = queue.Queue()
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self._progress_callback = None
        self._lock = threading.Lock()
        self._error: Exception | None = None
        self._transcript_text = ""
        self._next_chunk_index = 1
        self._completed_chunks = 0
        self._final_total_chunks = None
        self._last_activity_at = time.monotonic()
        self._producer_thread = threading.Thread(target=self._produce_chunks, daemon=True)
        self._worker_thread = threading.Thread(target=self._transcribe_chunks, daemon=True)
        self._producer_thread.start()
        self._worker_thread.start()

    def stop_recording_and_wait(self, progress_callback=None) -> str | None:
        self._progress_callback = progress_callback
        self._note_activity()
        self.recorder.stop()
        total_frames = self.recorder.total_frames()

        if total_frames == 0:
            self.stop_requested.set()
            self.chunk_queue.put(None)
            self.finished.set()
            self.recorder.clear()
            return None

        final_chunk_start = (self._next_chunk_index - 1) * self.step_frames
        if self._next_chunk_index == 1:
            self._final_total_chunks = 1
        else:
            has_final_partial = (total_frames - final_chunk_start) > self.overlap_frames
            self._final_total_chunks = self._next_chunk_index if has_final_partial else self._next_chunk_index - 1
        if self._progress_callback is not None:
            self._progress_callback("Finalizing transcript...")

        self.stop_requested.set()
        while not self.finished.wait(FINALIZE_POLL_INTERVAL_SECONDS):
            if self._error is not None and not self._worker_thread.is_alive():
                break
            if not self._worker_thread.is_alive():
                self._set_error(RuntimeError("Transcription worker exited unexpectedly while finalizing transcript."))
                self.finished.set()
                break
            if self._seconds_since_activity() >= FINALIZE_INACTIVITY_TIMEOUT_SECONDS:
                self._set_error(
                    RuntimeError(
                        "Transcription stopped making progress while finalizing. "
                        "If GPU acceleration is hanging, relaunch with `LDI_DEVICE=cpu` set in the environment."
                    )
                )
                self.finished.set()
                break

        self.recorder.clear()

        if self._error is not None:
            raise self._error
        return self._transcript_text.strip()

    def _produce_chunks(self) -> None:
        try:
            while not self.stop_requested.is_set():
                self._enqueue_ready_full_chunks()
                time.sleep(0.25)

            self._enqueue_ready_full_chunks()
            self._enqueue_final_chunk_if_needed()
        except Exception as exc:
            self._set_error(exc)
        finally:
            self.chunk_queue.put(None)

    def _enqueue_ready_full_chunks(self) -> None:
        while True:
            end_frame = ((self._next_chunk_index - 1) * self.step_frames) + self.chunk_frames
            if self.recorder.total_frames() < end_frame:
                return
            self._enqueue_chunk(self._next_chunk_index, end_frame - self.chunk_frames, end_frame)
            self._next_chunk_index += 1
            self.recorder.drop_before((self._next_chunk_index - 1) * self.step_frames)

    def _enqueue_final_chunk_if_needed(self) -> None:
        total_frames = self.recorder.total_frames()
        if total_frames <= 0:
            return

        start_frame = (self._next_chunk_index - 1) * self.step_frames
        if self._next_chunk_index == 1:
            self._enqueue_chunk(self._next_chunk_index, 0, total_frames)
            return

        if (total_frames - start_frame) <= self.overlap_frames:
            return
        self._enqueue_chunk(self._next_chunk_index, start_frame, total_frames)

    def _enqueue_chunk(self, index: int, start_frame: int, end_frame: int) -> None:
        audio = self.recorder.copy_range(start_frame, end_frame)
        self.chunk_queue.put(
            ChunkJob(
                index=index,
                start_frame=start_frame,
                end_frame=end_frame,
                audio=audio,
            )
        )

    def _transcribe_chunks(self) -> None:
        try:
            model = self.transcriber._load_model(progress_callback=None)
            while True:
                job = self.chunk_queue.get()
                if job is None or self._error is not None:
                    break

                self._emit_chunk_progress(job)
                if job.audio.size == 0:
                    chunk_text = ""
                else:
                    chunk_text = self.transcriber._transcribe_single_pass(
                        model,
                        job.audio[:, 0],
                        self.model_name,
                        self.device,
                        progress_callback=None,
                    )

                if self._error is not None:
                    break

                with self._lock:
                    self._transcript_text = self.transcriber._merge_chunk_text(self._transcript_text, chunk_text)
                    self._completed_chunks += 1
                    self._last_activity_at = time.monotonic()
        except Exception as exc:
            self._set_error(exc)
        finally:
            self.finished.set()

    def _emit_chunk_progress(self, job: ChunkJob) -> None:
        if self._progress_callback is None:
            return

        self._note_activity()
        start_seconds = job.start_frame / SAMPLE_RATE
        end_seconds = job.end_frame / SAMPLE_RATE
        total_chunks = self._final_total_chunks or "?"
        self._progress_callback(
            f"Transcribing chunk {job.index}/{total_chunks} "
            f"({self.transcriber._format_duration(start_seconds)}-{self.transcriber._format_duration(end_seconds)})..."
        )

    def _note_activity(self) -> None:
        with self._lock:
            self._last_activity_at = time.monotonic()

    def _seconds_since_activity(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity_at

    def _set_error(self, exc: Exception) -> None:
        with self._lock:
            if self._error is not None:
                return
            self._error = exc
