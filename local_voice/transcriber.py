from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import tempfile
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

from .paths import app_data_dir
from .inference import InferenceClient
from .settings import read_settings, write_settings

MODEL_OPTIONS = ["turbo", "large-v3", "medium", "small"]
MODEL_TITLES = {
    "turbo": "Turbo",
    "large-v3": "Large",
    "medium": "Medium",
    "small": "Small",
}
TRANSLATION_MODEL_OPTIONS = {"large-v3", "medium"}
DEFAULT_MODEL = "turbo"
APP_DIR = app_data_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
MODEL_DIR = APP_DIR / "models"
OVERLAP_WORD_LIMIT = 80
LANGUAGES = {"auto": "Auto", "en": "English", "he": "Hebrew"}
# Download metadata is kept lightweight: checking models must not import PyTorch in the UI.
MODEL_FILES = {
    "small": ("small.pt", "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"),
    "medium": ("medium.pt", "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1"),
    "large-v3": ("large-v3.pt", "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb"),
    "turbo": ("large-v3-turbo.pt", "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a"),
}


class WhisperTranscriber:
    def __init__(self) -> None:
        self._model = None
        self._whisper = None
        self._device = None
        settings = self._load_settings()
        self._model_name = self._preferred_model_name(settings)
        if os.environ.get("LDI_MODEL") in MODEL_OPTIONS:
            self._model_name = os.environ["LDI_MODEL"]
        self._translate_to_english = bool(settings.get("translate_to_english", False))
        self._windows_cpu_fallback = settings.get("windows_device") == "cpu"
        # Recordings detect their own language; obsolete UI preferences cannot pin it.
        # An explicit CLI/environment override remains available for diagnostics.
        self._language = os.environ.get("LDI_LANGUAGE", "auto")
        if self._language not in LANGUAGES:
            self._language = "auto"
        self._lock = threading.Lock()
        self._worker = InferenceClient()

    def transcribe(self, audio_path: Path, progress_callback=None) -> str:
        # ffmpeg also accepts compressed formats and resamples external recordings.
        result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio_path),
                                 "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                                capture_output=True, check=True)
        with tempfile.TemporaryDirectory(prefix="local-voice-") as directory:
            raw = Path(directory) / "audio.f32"
            raw.write_bytes(result.stdout)
            audio_frames = len(result.stdout) // 4
            language = self.language()
            text = ""
            for start in range(0, audio_frames, 30 * 16000):
                chunk = self.transcribe_chunk(raw, start, min(start + 30 * 16000, audio_frames),
                                              language=language, progress_callback=progress_callback)
                if chunk["text"]:
                    language = language or chunk.get("language")
                    text = (text + " " + chunk["text"]).strip()
            return text

    def language(self):
        return None if self._language == "auto" else self._language

    def save_language_preference(self, language):
        if language not in LANGUAGES:
            raise ValueError(f"Unsupported language: {language}")
        self._language = language
        self._write_settings({"language": language})

    def backend_name(self):
        selected = os.environ.get("LDI_BACKEND")
        if selected and selected not in {"mlx", "torch"}:
            raise ValueError("LDI_BACKEND must be mlx or torch")
        if selected:
            return selected
        if sys.platform == "darwin" and platform.machine() == "arm64" and os.environ.get("LDI_DEVICE", "mps") == "mps":
            return "mlx"
        return "torch"

    def _worker_payload(self):
        whisper = self._load_whisper_module()
        self._ensure_model_downloaded(whisper, self._model_name)
        return dict(model=self._model_name, backend=self.backend_name(), device=self._default_device(),
                    checkpoint=str(self._model_target_path(whisper, self._model_name)),
                    mlx_path=str(MODEL_DIR / "mlx" / self._model_name))

    def transcribe_chunk(self, audio_path, start_frame, end_frame, *, language=None, task=None,
                         cancel=None, progress_callback=None):
        payload = self._worker_payload()
        payload.update(op="transcribe", audio_path=str(audio_path), start_frame=start_frame, end_frame=end_frame,
                       language=language, task=task or ("translate" if self.translate_to_english_enabled() else "transcribe"))
        return self._worker.request(payload, cancel=cancel, progress_callback=progress_callback)

    def _load_model(self, progress_callback=None, cancel=None):
        with self._lock:
            if self._model is None:
                payload = self._worker_payload()
                payload["op"] = "load"
                self._worker.request(payload, progress_callback=progress_callback, cancel=cancel)
                self._model = self._worker
            return self._model

    def _load_whisper_module(self, progress_callback=None):
        if self._whisper is None:
            self._whisper = SimpleNamespace(_MODELS={
                name: f"https://openaipublic.azureedge.net/main/whisper/models/{sha}/{filename}"
                for name, (filename, sha) in MODEL_FILES.items()
            })
        return self._whisper

    def _default_device(self) -> str:
        if self._device is not None:
            return self._device

        override = os.environ.get("LDI_DEVICE")
        if override:
            self._device = override
            return self._device

        if sys.platform == "win32" and self._windows_cpu_fallback:
            self._device = "cpu"
            return self._device

        if sys.platform == "darwin" and platform.machine() == "x86_64":
            self._device = "cpu"
            return self._device

        if sys.platform == "darwin" and platform.machine() == "arm64" and self.backend_name() == "mlx":
            self._device = "mps"
            return self._device

        import torch

        mps_backend = getattr(torch.backends, "mps", None)
        if sys.platform == "darwin" and mps_backend is not None and mps_backend.is_available():
            self._device = "mps"
        elif torch.cuda.is_available():
            self._device = "cuda"
        else:
            self._device = "cpu"
        return self._device

    def _default_model_name(self) -> str:
        return self._model_name

    def current_model_name(self) -> str:
        return self._model_name

    def current_model_supports_translation(self) -> bool:
        return self.model_supports_translation(self._model_name)

    def model_supports_translation(self, model_name: str) -> bool:
        return model_name in TRANSLATION_MODEL_OPTIONS

    def translate_to_english_enabled(self) -> bool:
        return self.translate_to_english_enabled_for_model(self._model_name)

    def translate_to_english_enabled_for_model(self, model_name: str) -> bool:
        return self._translate_to_english and self.model_supports_translation(model_name)

    def set_translate_to_english(self, enabled: bool) -> None:
        self._translate_to_english = bool(enabled)

    def set_model_name(self, model_name: str) -> None:
        if model_name not in MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model_name}")

        with self._lock:
            if self._model_name == model_name:
                return
            self._release_model_locked()
            self._model_name = model_name

    def switch_model(self, model_name: str, progress_callback=None):
        if model_name not in MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model_name}")

        with self._lock:
            if self._model_name == model_name and self._model is not None:
                return self._model
            if self._model_name != model_name:
                self._release_model_locked()
                self._model_name = model_name

        return self._load_model(progress_callback=progress_callback)

    def save_model_preference(self, model_name: str) -> None:
        if model_name not in MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model_name}")

        self._write_settings({"model": model_name, "translate_to_english": self._translate_to_english})

    def save_translate_preference(self, enabled: bool) -> None:
        self.set_translate_to_english(enabled)
        self._write_settings({"model": self._model_name, "translate_to_english": self._translate_to_english})

    def unload_model(self) -> None:
        with self._lock:
            self._release_model_locked()

    def _release_model_locked(self) -> None:
        self._model = None
        self._worker.close()

    def is_model_cached(self, model_name: str) -> bool:
        whisper = self._load_whisper_module()
        target = self._model_target_path(whisper, model_name)
        return target.is_file()

    def download_model(self, model_name: str, progress_callback=None, cancel=None) -> Path:
        if model_name not in MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model_name}")

        whisper = self._load_whisper_module(progress_callback=progress_callback)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        from filelock import FileLock
        with FileLock(str(self._model_target_path(whisper, model_name)) + ".download.lock"):
            self._download_model_file(whisper, model_name, progress_callback=progress_callback, cancel=cancel)
        return self._model_target_path(whisper, model_name)

    def startup_message(self) -> str:
        model_name = self._default_model_name()
        device = self._default_device()
        if self._model is None:
            return f"Loading {model_name} on {device}..."
        verb = "Translating" if self.translate_to_english_enabled() else "Transcribing"
        return f"{verb} on {model_name} ({device})..."

    def _ensure_model_downloaded(self, whisper, model_name: str, progress_callback=None) -> None:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        url = whisper._MODELS[model_name]
        expected_sha256 = url.split("/")[-2]
        target = self._model_target_path(whisper, model_name)
        marker = self._verification_marker_path(target)

        if target.is_file():
            if self._is_verified_cache_valid(target, marker, expected_sha256):
                return
            if progress_callback is not None:
                progress_callback(f"Verifying cached {model_name}...")
            if self._sha256(target) == expected_sha256:
                self._write_verification_marker(target, marker, expected_sha256)
                return
            target.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            raise RuntimeError(self._missing_model_message(model_name))

        raise RuntimeError(self._missing_model_message(model_name))

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verification_marker_path(self, target: Path) -> Path:
        return target.with_suffix(f"{target.suffix}.verified.json")

    def _model_target_path(self, whisper, model_name: str) -> Path:
        url = whisper._MODELS[model_name]
        return MODEL_DIR / Path(url).name

    def _is_verified_cache_valid(self, target: Path, marker: Path, expected_sha256: str) -> bool:
        if not marker.is_file():
            return False

        try:
            payload = json.loads(marker.read_text())
        except Exception:
            return False

        stat = target.stat()
        return (
            payload.get("sha256") == expected_sha256
            and payload.get("size") == stat.st_size
            and payload.get("mtime_ns") == stat.st_mtime_ns
        )

    def _write_verification_marker(self, target: Path, marker: Path, sha256: str) -> None:
        stat = target.stat()
        payload = {
            "sha256": sha256,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        marker.write_text(json.dumps(payload))

    def _download_model_file(self, whisper, model_name: str, progress_callback=None, cancel=None) -> None:
        url = whisper._MODELS[model_name]
        expected_sha256 = url.split("/")[-2]
        target = self._model_target_path(whisper, model_name)
        marker = self._verification_marker_path(target)

        if target.is_file():
            if self._is_verified_cache_valid(target, marker, expected_sha256):
                if progress_callback is not None:
                    progress_callback(f"{model_name} already cached.")
                return
            if progress_callback is not None:
                progress_callback(f"Verifying cached {model_name}...")
            if self._sha256(target) == expected_sha256:
                self._write_verification_marker(target, marker, expected_sha256)
                if progress_callback is not None:
                    progress_callback(f"{model_name} already cached.")
                return

        partial = target.with_suffix(f"{target.suffix}.part")

        if progress_callback is not None:
            progress_callback(f"Downloading {model_name}...")

        try:
            if not partial.is_file() or self._sha256(partial) != expected_sha256:
                from .downloads import download
                download(url, partial, progress=progress_callback, cancel=cancel)
        except Exception as exc:
            raise RuntimeError(str(exc) or "Failed to download Whisper model.") from exc

        if progress_callback is not None:
            progress_callback(f"Verifying downloaded {model_name}...")

        actual_sha256 = self._sha256(partial)
        if actual_sha256 != expected_sha256:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Downloaded Whisper model failed checksum verification.")

        partial.replace(target)
        self._write_verification_marker(target, marker, expected_sha256)

    def _missing_model_message(self, model_name: str) -> str:
        return (
            f"{model_name} is not cached. Run `./start.cmd --download-model {model_name}` first."
        )

    def _load_settings(self) -> dict:
        return read_settings(SETTINGS_PATH)

    def _preferred_model_name(self, settings: dict) -> str:
        model_name = settings.get("model")
        if model_name in MODEL_OPTIONS:
            return model_name
        return DEFAULT_MODEL

    def _write_settings(self, payload: dict) -> None:
        write_settings(payload, SETTINGS_PATH)

    def _timestamp_now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _write_json(self, path: Path, payload: dict) -> None:
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _append_jsonl(self, path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_text(self, path: Path, text: str) -> None:
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def _format_duration(self, seconds: float) -> str:
        total_seconds = max(0, int(round(seconds)))
        minutes, remaining = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{remaining:02d}"
        return f"{minutes}:{remaining:02d}"

    def _merge_chunk_text(self, existing: str, incoming: str) -> str:
        existing = existing.strip()
        incoming = incoming.strip()
        if not existing:
            return incoming
        if not incoming:
            return existing

        existing_words = existing.split()
        incoming_words = incoming.split()
        overlap_count = self._find_word_overlap(existing_words, incoming_words)

        if overlap_count > 0:
            merged_words = existing_words + incoming_words[overlap_count:]
            return " ".join(merged_words).strip()

        return f"{existing} {incoming}".strip()

    def _find_word_overlap(self, existing_words: list[str], incoming_words: list[str]) -> int:
        max_overlap = min(OVERLAP_WORD_LIMIT, len(existing_words), len(incoming_words))
        if max_overlap <= 0:
            return 0

        normalized_existing = [self._normalize_overlap_word(word) for word in existing_words[-OVERLAP_WORD_LIMIT:]]
        normalized_incoming = [self._normalize_overlap_word(word) for word in incoming_words[:OVERLAP_WORD_LIMIT]]

        for overlap_count in range(max_overlap, 0, -1):
            if normalized_existing[-overlap_count:] == normalized_incoming[:overlap_count]:
                return overlap_count
        return 0

    def _normalize_overlap_word(self, word: str) -> str:
        return re.sub(r"^\W+|\W+$", "", word, flags=re.UNICODE).casefold()
