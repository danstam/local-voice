from __future__ import annotations

import json
import os
import threading

import numpy as np

from .workspace import SessionWorkspace, merge_result
from .inference import TranscriptionCancelled
from .recorder import SAMPLE_RATE
from .speech import SpeechSegmenter


class TranscriptionFailed(RuntimeError):
    def __init__(self, message, partial_text):
        super().__init__(message)
        self.partial_text = partial_text


class ChunkedRecordingSession:
    """Spool audio independently of inference; pending jobs contain no audio arrays."""
    def __init__(self, recorder, transcriber, progress_callback=None, *, workspace_root=None, segmenter=None,
                 error_callback=None):
        self.recorder = recorder
        self.transcriber = transcriber
        self.model_name = transcriber.current_model_name()
        self.language = transcriber.language()
        self.task = "translate" if transcriber.translate_to_english_enabled() else "transcribe"
        self.workspace = SessionWorkspace(workspace_root)
        self.segmenter = segmenter if segmenter is not None else SpeechSegmenter()
        self.stop_requested = threading.Event()
        self.cancelled = threading.Event()
        self._abort_inference = threading.Event()
        self._capture_failed = False
        self.finished = threading.Event()
        self._condition = threading.Condition()
        self._lifecycle = threading.Lock()
        self._producer_done = False
        self._progress_callback = progress_callback
        self._error_callback = error_callback
        self._error = None
        self._transcript_text = ""
        self._enqueued = 0
        self._completed = 0
        self._spooled = 0
        self._producer_thread = threading.Thread(target=self._produce_chunks, daemon=True)
        self._worker_thread = threading.Thread(target=self._transcribe_chunks, daemon=True)
        self._producer_thread.start()
        self._worker_thread.start()

    def _emit(self, message):
        if self._progress_callback is not None and not self.cancelled.is_set():
            self._progress_callback(message)

    def stop_recording_and_wait(self, progress_callback=None):
        if progress_callback is not None:
            self._progress_callback = progress_callback
        with self._lifecycle:
            if self.cancelled.is_set():
                raise TranscriptionCancelled("Recording cancelled")
            try:
                self.recorder.stop()
            except RuntimeError:
                if not self._capture_failed:
                    raise
            self.stop_requested.set()
            self._producer_thread.join()
            self.recorder.clear()
        self._emit("Finalizing transcript...")
        try:
            self.finished.wait()  # InferenceClient enforces deadlines and kills hangs.
            if self.cancelled.is_set():
                raise TranscriptionCancelled("Recording cancelled")
            if self._error is not None:
                raise TranscriptionFailed(str(self._error), self._transcript_text) from self._error
            return self._transcript_text.strip() if self._spooled else None
        finally:
            with self._lifecycle:
                if not self.cancelled.is_set():
                    self.workspace.close()

    def cancel(self):
        if self.workspace.closed:
            return
        self.cancelled.set()
        self._abort_inference.set()
        with self._lifecycle:
            if self.workspace.closed:
                return
            try:
                self.recorder.stop()
            except RuntimeError:
                pass
            self.stop_requested.set()
            self._producer_thread.join()
            self.recorder.clear()
        with self._condition:
            self._condition.notify_all()
        self._worker_thread.join()
        self.workspace.close()

    def _enqueue(self, ranges):
        for start, end in ranges:
            self.workspace.job(self._enqueued + 1, start, end)
            with self._condition:
                self._enqueued += 1
                self._condition.notify_all()

    def _produce_chunks(self):
        try:
            with self.workspace.audio_path.open("wb") as audio_file:
                while True:
                    total = self.recorder.total_frames()
                    if total > self._spooled:
                        end = min(total, self._spooled + SAMPLE_RATE)
                        audio = self.recorder.copy_range(self._spooled, end)[:, 0]
                        if len(audio) != end - self._spooled:
                            raise RuntimeError("Microphone audio buffer was cleared before it was saved")
                        audio_file.write(audio.astype("<f4", copy=False).tobytes())
                        audio_file.flush()
                        self._spooled = end
                        self.recorder.drop_before(end)
                        if not self.cancelled.is_set():
                            self._enqueue(self.segmenter.feed(audio))
                        continue
                    if self.stop_requested.is_set():
                        if not self.cancelled.is_set():
                            self._enqueue(self.segmenter.finish())
                        os.fsync(audio_file.fileno())
                        break
                    self.stop_requested.wait(0.1)
        except Exception as exc:
            self._error = exc
            self._capture_failed = True
            self._abort_inference.set()
            self.stop_requested.set()
            try:
                self.recorder.stop()
            except RuntimeError:
                pass
            if self._error_callback is not None:
                self._error_callback(f"Recording stopped because audio could not be saved: {exc}")
        finally:
            with self._condition:
                self._producer_done = True
                self._condition.notify_all()

    def _transcribe_chunks(self):
        detected_language = self.language
        previous_end = 0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self.cancelled.is_set() or self._error is not None
                                             or self._completed < self._enqueued or self._producer_done)
                    if self.cancelled.is_set() or self._error is not None:
                        break
                    if self._completed == self._enqueued and self._producer_done:
                        break
                    index = self._completed + 1
                job = json.loads((self.workspace.path / "jobs" / f"{index:06d}.json").read_text())
                self._emit(f"Transcribing {index}/{self._enqueued} · {self._enqueued - index} waiting")
                result = self.transcriber.transcribe_chunk(
                    self.workspace.audio_path, job["start_frame"], job["end_frame"],
                    language=detected_language, task=self.task, cancel=self._abort_inference,
                    progress_callback=self._emit,
                )
                if self.cancelled.is_set():
                    break
                if result["text"] and detected_language is None:
                    detected_language = result.get("language")
                self._transcript_text = merge_result(self.transcriber, self._transcript_text, result, job, previous_end)
                previous_end = job["end_frame"]
                seconds = max(0.001, (job["end_frame"] - job["start_frame"]) / SAMPLE_RATE)
                result["realtime_factor"] = result["seconds"] / seconds
                result.update(start_frame=job["start_frame"], end_frame=job["end_frame"])
                self._completed = index
                (self.workspace.path / "jobs" / f"{index:06d}.json").unlink()
                self._emit(f"Transcribed {index}/{self._enqueued} · {result['seconds']:.1f}s · "
                           f"{result['realtime_factor']:.2f}× real time · {result.get('decode_attempts', 0)} passes")
        except TranscriptionCancelled:
            pass
        except Exception as exc:
            self._error = exc
            self._emit("Transcription failed. Press Stop to view completed text.")
        finally:
            self.finished.set()
