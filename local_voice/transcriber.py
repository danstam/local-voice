from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .paths import app_data_dir

MODEL_OPTIONS = ["turbo", "large-v3", "medium", "small"]
MODEL_TITLES = {
    "turbo": "Turbo",
    "large-v3": "Large",
    "medium": "Medium",
    "small": "Small",
}
TRANSLATION_MODEL_OPTIONS = {"large-v3", "medium"}
DEFAULT_MODEL = "small"
APP_DIR = app_data_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
MODEL_DIR = APP_DIR / "models"
OVERLAP_WORD_LIMIT = 80


class WhisperTranscriber:
    def __init__(self) -> None:
        self._model = None
        self._whisper = None
        self._device = None
        settings = self._load_settings()
        self._model_name = self._preferred_model_name(settings)
        self._translate_to_english = bool(settings.get("translate_to_english", False))
        self._lock = threading.Lock()

    def transcribe(self, audio_path: Path, progress_callback=None) -> str:
        model_name = self._default_model_name()
        device = self._default_device()
        model = self._load_model(progress_callback=progress_callback)
        return self._transcribe_single_pass(model, str(audio_path), model_name, device, progress_callback)

    def _transcribe_single_pass(self, model, audio_input, model_name: str, device: str, progress_callback=None) -> str:
        task = "translate" if self.translate_to_english_enabled_for_model(model_name) else "transcribe"
        if progress_callback is not None:
            verb = "Translating" if task == "translate" else "Transcribing"
            progress_callback(f"{verb} on {model_name} ({device})...")

        result = model.transcribe(
            audio_input,
            fp16=False,
            verbose=False,
            condition_on_previous_text=False,
            task=task,
        )
        return result["text"].strip()

    def _load_model(self, progress_callback=None):
        with self._lock:
            if self._model is None:
                model_name = self._default_model_name()
                whisper = self._load_whisper_module(progress_callback=progress_callback)
                MODEL_DIR.mkdir(parents=True, exist_ok=True)
                self._ensure_model_downloaded(whisper, model_name, progress_callback=progress_callback)
                if progress_callback is not None:
                    progress_callback(f"Loading {model_name} on {self._default_device()}...")
                self._model = whisper.load_model(
                    model_name,
                    device=self._default_device(),
                    download_root=str(MODEL_DIR),
                )
            return self._model

    def _load_whisper_module(self, progress_callback=None):
        if self._whisper is None:
            if progress_callback is not None:
                progress_callback("Importing Whisper runtime...")
            import whisper

            self._whisper = whisper
        return self._whisper

    def _default_device(self) -> str:
        if self._device is not None:
            return self._device

        override = os.environ.get("LDI_DEVICE")
        if override:
            self._device = override
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
        previous = self._model
        self._model = None
        if previous is None:
            return

        del previous
        gc.collect()

    def is_model_cached(self, model_name: str) -> bool:
        whisper = self._load_whisper_module()
        target = self._model_target_path(whisper, model_name)
        return target.is_file()

    def download_model(self, model_name: str, progress_callback=None) -> Path:
        if model_name not in MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model_name}")

        whisper = self._load_whisper_module(progress_callback=progress_callback)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self._download_model_file(whisper, model_name, progress_callback=progress_callback)
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

    def _download_model_file(self, whisper, model_name: str, progress_callback=None) -> None:
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
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)

        if progress_callback is not None:
            progress_callback(f"Downloading {model_name}...")

        try:
            self._download_url(url, partial)
        except Exception as exc:
            partial.unlink(missing_ok=True)
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
            f"{model_name} is not cached. Run `./voice --download-model {model_name}` on macOS or "
            f"`.\\voice_windows.bat --download-model {model_name}` on Windows first."
        )

    def _download_url(self, url: str, target: Path) -> None:
        errors: list[str] = []

        curl_error = self._download_with_curl(url, target, allow_insecure=False)
        if curl_error is None:
            return
        if curl_error:
            errors.append(curl_error)

        curl_insecure_error = self._download_with_curl(url, target, allow_insecure=True)
        if curl_insecure_error is None:
            return
        if curl_insecure_error:
            errors.append(curl_insecure_error)

        try:
            self._download_with_python(url, target, allow_insecure=False)
            return
        except Exception as exc:
            errors.append(str(exc))

        try:
            self._download_with_python(url, target, allow_insecure=True)
            return
        except Exception as exc:
            errors.append(str(exc))

        joined = "; ".join(error for error in errors if error)
        raise RuntimeError(joined or "Failed to download Whisper model.")

    def _download_with_curl(self, url: str, target: Path, allow_insecure: bool) -> str | None:
        curl = shutil.which("curl")
        if curl is None:
            return "curl is not installed"

        command = [curl, "-L", "--fail"]
        if allow_insecure:
            command.append("-k")
        command.extend(["-o", str(target), url])
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return None
        return result.stderr.strip() or result.stdout.strip() or "curl download failed"

    def _download_with_python(self, url: str, target: Path, allow_insecure: bool) -> None:
        context = ssl._create_unverified_context() if allow_insecure else None
        try:
            with urlopen(url, context=context) as response, target.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (HTTPError, URLError, OSError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _load_settings(self) -> dict:
        try:
            payload = json.loads(SETTINGS_PATH.read_text())
        except Exception:
            return {}

        if isinstance(payload, dict):
            return payload
        return {}

    def _preferred_model_name(self, settings: dict) -> str:
        model_name = settings.get("model")
        if model_name in MODEL_OPTIONS:
            return model_name
        return DEFAULT_MODEL

    def _write_settings(self, payload: dict) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(payload))

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

        normalized_existing = [self._normalize_overlap_word(word) for word in existing_words]
        normalized_incoming = [self._normalize_overlap_word(word) for word in incoming_words]

        for overlap_count in range(max_overlap, 0, -1):
            if normalized_existing[-overlap_count:] == normalized_incoming[:overlap_count]:
                return overlap_count
        return 0

    def _normalize_overlap_word(self, word: str) -> str:
        return re.sub(r"^\W+|\W+$", "", word, flags=re.UNICODE).casefold()
