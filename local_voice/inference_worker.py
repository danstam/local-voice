"""Private JSON-lines worker; stdout is reserved for the protocol."""
from __future__ import annotations

import contextlib
import gc
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np


_PROTOCOL_STDOUT = sys.stdout


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}, ensure_ascii=False), file=_PROTOCOL_STDOUT, flush=True)


def progress(message):
    emit("progress", message=message)


def converted_model_exists(target):
    return (target / "config.json").is_file() and any(
        (target / filename).is_file() for filename in ("weights.safetensors", "weights.npz"))


def convert_checkpoint(checkpoint_path, target):
    from filelock import FileLock

    target = Path(target)
    if converted_model_exists(target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".lock", timeout=300):
        _convert_checkpoint(checkpoint_path, target)


def _convert_checkpoint(checkpoint_path, target):
    """Convert a verified local checkpoint once; no network or second model download.

    Tensor names/layout follow Apple's MIT-licensed mlx-examples/whisper/convert.py.
    """
    target = Path(target)
    if converted_model_exists(target):
        return
    import mlx.core as mx
    import torch
    target.mkdir(parents=True, exist_ok=True)
    progress("Converting cached model to MLX (once)...")
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True, mmap=True)
    weights = {}
    for index, (name, tensor) in enumerate(checkpoint["model_state_dict"].items()):
        if name == "encoder.positional_embedding":
            continue
        name = name.replace("mlp.0", "mlp1").replace("mlp.2", "mlp2")
        array = tensor.detach().numpy()
        if "conv" in name and array.ndim == 3:
            array = array.swapaxes(1, 2)
        weights[name] = mx.array(np.ascontiguousarray(array)).astype(mx.float16)
        if index % 100 == 0:
            mx.eval(list(weights.values()))
            progress(f"Converted {index + 1} weight tensors...")
    # Passing tensors as keyword arguments hits nanobind's 1024-argument limit
    # on Large-v3. Safetensors takes a dictionary and supports all model sizes.
    temporary = target / "weights.partial.safetensors"
    mx.save_safetensors(str(temporary), weights)
    temporary.replace(target / "weights.safetensors")
    config = {**checkpoint["dims"], "model_type": "whisper"}
    temporary_config = target / "config.json.tmp"
    temporary_config.write_text(json.dumps(config), encoding="utf-8")
    temporary_config.replace(target / "config.json")
    del weights, checkpoint
    gc.collect()
    mx.clear_cache()


class Runtime:
    def __init__(self):
        self.key = None
        self.model = None

    def load(self, request):
        key = (request["backend"], request["model"], request["device"], request["checkpoint"])
        if key == self.key:
            return
        if self.key is not None:
            raise RuntimeError("Restart the inference worker before switching models")
        self.backend = request["backend"]
        self.device = request["device"]
        progress(f"Loading {request['model']} with {self.backend}...")
        if self.backend == "mlx":
            convert_checkpoint(request["checkpoint"], request["mlx_path"])
            import mlx.core as mx
            self.module = importlib.import_module("mlx_whisper.transcribe")
            self.model_path = request["mlx_path"]
            self.model = self.module.ModelHolder.get_model(self.model_path, mx.float16)
        else:
            import torch
            import whisper
            # Load on CPU first: avoid a GPU copy of the checkpoint plus the model.
            self.model = whisper.load_model(request["checkpoint"], device="cpu")
            self.model.to(self.device).eval()
        self.key = key

    def transcribe(self, audio, language, task):
        attempts = 0
        if self.backend == "mlx":
            import mlx.core as mx
            mx.reset_peak_memory()
        original_decode = self.model.decode

        def observed_decode(*args, **kwargs):
            nonlocal attempts
            result = original_decode(*args, **kwargs)
            attempts += 1
            progress(f"Decoded pass {attempts}")
            return result

        self.model.decode = observed_decode
        started = time.monotonic()
        try:
            # Retain confidence-based fallback, bounded to two attempts per window.
            options = dict(language=language, task=task, verbose=None,
                           condition_on_previous_text=False, temperature=(0.0, 0.2))
            if self.backend == "mlx":
                result = self.module.transcribe(audio, path_or_hf_repo=self.model_path, fp16=True, **options)
            else:
                result = self.model.transcribe(audio, fp16=self.device == "cuda", **options)
        finally:
            self.model.decode = original_decode
            if self.backend == "mlx":
                import mlx.core as mx
                mx.clear_cache()
        segments = result.get("segments", [])
        memory = {}
        if self.backend == "mlx":
            memory["mlx_peak_mb"] = mx.get_peak_memory() / 1024**2
            memory["mlx_active_mb"] = mx.get_active_memory() / 1024**2
        try:
            import resource
            memory["process_peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
                1024**2 if sys.platform == "darwin" else 1024)
        except ImportError:
            pass  # resource is not available on Windows.
        return {
            "text": result["text"].strip(), "language": result.get("language"),
            "segments": [{k: s[k] for k in ("start", "end", "text")} for s in segments],
            "decode_attempts": attempts,
            "fallback_segments": sum(s.get("temperature", 0) > 0 for s in segments),
            "seconds": time.monotonic() - started,
            "memory": memory,
        }

    def handle(self, request):
        self.load(request)
        if request["op"] == "load":
            progress("Warming up inference...")
            self.transcribe(np.zeros(16000, dtype=np.float32), "en", "transcribe")
            return {"ready": True, "backend": self.backend}
        count = request["end_frame"] - request["start_frame"]
        audio = np.fromfile(request["audio_path"], dtype="<f4", count=count,
                            offset=request["start_frame"] * 4)
        if len(audio) != count:
            raise RuntimeError("Saved audio is incomplete; refusing to transcribe a truncated chunk")
        return self.transcribe(audio, request.get("language"), request.get("task", "transcribe"))


def main():
    from tqdm import tqdm
    # tqdm otherwise constructs a multiprocessing RLock even with verbose=None.
    # The worker is single-threaded and needs no process-shared semaphore.
    tqdm.set_lock(threading.RLock())
    runtime = Runtime()
    # Dependencies must never contact the Hub during normal recording.
    os.environ["HF_HUB_OFFLINE"] = "1"
    for line in sys.stdin:
        try:
            request = json.loads(line)
            # Libraries occasionally print status text: route it away from the protocol.
            with contextlib.redirect_stdout(sys.stderr):
                # Emit protocol progress using the original stdout inside this context.
                result = runtime.handle(request)
            emit("result", result=result)
        except Exception as exc:
            emit("error", message=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
