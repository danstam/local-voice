"""Shared maintenance commands, usable without starting the desktop UI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import time

from .transcriber import LANGUAGES, MODEL_OPTIONS, WhisperTranscriber


def arguments(parser):
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--download-model", choices=MODEL_OPTIONS)
    actions.add_argument("--benchmark", type=Path, metavar="AUDIO")
    parser.add_argument("--model", choices=MODEL_OPTIONS)
    parser.add_argument("--backend", choices=["mlx", "torch"])
    parser.add_argument("--language", choices=list(LANGUAGES))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, help="Save benchmark measurements as JSON")


def handle(args):
    if args.backend:
        os.environ["LDI_BACKEND"] = args.backend
    if not (args.download_model or args.benchmark):
        if args.model:
            os.environ["LDI_MODEL"] = args.model
        if args.language:
            os.environ["LDI_LANGUAGE"] = args.language
        return False
    transcriber = WhisperTranscriber()
    if args.model:
        transcriber.set_model_name(args.model)
    if args.language:
        transcriber._language = args.language  # CLI overrides never change saved preferences.
    try:
        if args.download_model:
            print(f"Ready: {transcriber.download_model(args.download_model, progress_callback=print)}")
        else:
            if args.runs < 1:
                raise ValueError("--runs must be at least 1")
            import numpy as np
            import soundfile as sf
            from scipy.signal import resample_poly
            import math
            import tempfile

            audio, rate = sf.read(args.benchmark, dtype="float32", always_2d=True)
            audio = audio.mean(axis=1)
            divisor = math.gcd(rate, 16000)
            audio = resample_poly(audio, 16000 // divisor, rate // divisor).astype("<f4")
            if not len(audio):
                raise ValueError("Benchmark audio is empty")
            with tempfile.TemporaryDirectory(prefix="local-voice-benchmark-") as temporary:
                raw = Path(temporary) / "audio.f32"
                audio.tofile(raw)
                started = time.monotonic()
                transcriber._load_model(progress_callback=print)
                warmup = time.monotonic() - started
                runs = []
                for index in range(args.runs):
                    started = time.monotonic()
                    text = ""
                    attempts = 0
                    peak_memory = {}
                    language = transcriber.language()
                    for start in range(0, len(audio), 30 * 16000):
                        result = transcriber.transcribe_chunk(raw, start, min(len(audio), start + 30 * 16000),
                                                              language=language, progress_callback=print)
                        language = language or result.get("language")
                        text = (text + " " + result["text"]).strip()
                        attempts += result["decode_attempts"]
                        for key, value in result.get("memory", {}).items():
                            peak_memory[key] = max(peak_memory.get(key, 0), value)
                    elapsed = time.monotonic() - started
                    runs.append(dict(seconds=elapsed, realtime_factor=elapsed / (len(audio) / 16000),
                                     text=text, language=language, decode_attempts=attempts, memory=peak_memory))
                    print(f"Run {index + 1}: {elapsed:.2f}s")
            times = [run["seconds"] for run in runs]
            report = dict(model=transcriber.current_model_name(), backend=transcriber.backend_name(),
                          device=transcriber._default_device(), audio_seconds=len(audio) / 16000,
                          load_and_warmup_seconds=warmup, median_seconds=statistics.median(times),
                          p95_seconds=float(np.percentile(times, 95)), runs=runs)
            if args.output:
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        transcriber.unload_model()
    return True


def main():
    parser = argparse.ArgumentParser(description="Local Voice downloads and benchmarks")
    arguments(parser)
    args = parser.parse_args()
    if not handle(args):
        parser.print_help()


if __name__ == "__main__":
    main()
