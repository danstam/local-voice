"""Shared atomic preferences, preserving keys written by other app components."""
import json
from pathlib import Path
import uuid

from .paths import app_data_dir


def read_settings(path=None):
    path = Path(path) if path is not None else app_data_dir() / "settings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(payload, path=None):
    from filelock import FileLock
    path = Path(path) if path is not None else app_data_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        settings = read_settings(path)
        settings.update(payload)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
