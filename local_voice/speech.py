"""Conservative speech segmentation with padding and bounded boundary overlap."""
from __future__ import annotations

import numpy as np
import webrtcvad

SAMPLE_RATE = 16000
FRAME_SAMPLES = 480  # WebRTC requires 10, 20 or 30 ms of signed 16-bit PCM.


class SpeechSegmenter:
    def __init__(self, detector=None, max_seconds=30, min_seconds=5, silence_seconds=0.75):
        self.detector = detector if detector is not None else webrtcvad.Vad(1)
        self.maximum = int(max_seconds * SAMPLE_RATE)
        self.minimum = int(min_seconds * SAMPLE_RATE)
        self.silence = int(silence_seconds * SAMPLE_RATE)
        self.padding = int(0.3 * SAMPLE_RATE)
        self.overlap = SAMPLE_RATE
        self.pending = np.empty(0, dtype=np.float32)
        self.position = 0
        self.total = 0
        self.start = None
        self.last_voice = 0
        self.voiced = 0

    def feed(self, audio):
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.total += len(audio)
        data = np.concatenate((self.pending, audio))
        jobs = []
        usable = len(data) // FRAME_SAMPLES * FRAME_SAMPLES
        for offset in range(0, usable, FRAME_SAMPLES):
            frame = data[offset:offset + FRAME_SAMPLES]
            pcm = (np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes()
            voiced = self.detector.is_speech(pcm, SAMPLE_RATE)
            end = self.position + FRAME_SAMPLES
            if voiced:
                if self.start is None:
                    self.start = max(0, self.position - self.padding)
                self.last_voice = end
                self.voiced += FRAME_SAMPLES
            if self.start is not None:
                hard_cut = end - self.start >= self.maximum
                quiet_cut = end - self.last_voice >= self.silence and end - self.start >= self.minimum
                if hard_cut or quiet_cut:
                    # A few extra samples would make Whisper encode a second padded window.
                    cut = self.start + self.maximum if hard_cut else min(end, self.last_voice + self.padding)
                    if self.voiced >= FRAME_SAMPLES * 3:
                        jobs.append((self.start, cut))
                    self.start = cut - self.overlap if hard_cut else None
                    self.voiced = 0
            self.position = end
        self.pending = data[usable:].copy()
        return jobs

    def finish(self):
        # Process a short final VAD frame with zero padding; never invent saved audio.
        total = self.total
        jobs = []
        if len(self.pending):
            padding = FRAME_SAMPLES - len(self.pending)
            jobs.extend(self.feed(np.zeros(padding, dtype=np.float32)))
        if self.start is not None and self.voiced >= FRAME_SAMPLES * 3:
            jobs.append((self.start, total))
        self.start = None
        return [(start, min(end, total)) for start, end in jobs if start < total]
