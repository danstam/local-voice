from __future__ import annotations

import json
from pathlib import Path

from .paths import app_data_dir

_PROMPTS_FILE = "prompts.json"


def _prompts_path() -> Path:
    return app_data_dir() / _PROMPTS_FILE


def load_prompts() -> list[dict]:
    path = _prompts_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    result = []
    for entry in raw:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("text"), str)
        ):
            result.append({"name": entry["name"], "text": entry["text"]})
    return result


def save_prompts(prompts: list[dict]) -> None:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (_PROMPTS_FILE + ".tmp")
    tmp.write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(directory / _PROMPTS_FILE)


def add_prompt(name: str, text: str) -> list[dict]:
    prompts = load_prompts()
    prompts.append({"name": name.strip(), "text": text.strip()})
    save_prompts(prompts)
    return prompts


def pin_transcript(text: str) -> list[dict]:
    """Keep a transcript once, reusing the existing saved-prompts format."""
    text = text.strip()
    if not text:
        raise ValueError("Cannot pin an empty transcript")
    prompts = load_prompts()
    if any(entry["text"].strip() == text for entry in prompts):
        return prompts
    title = " ".join(text.split())
    if len(title) > 60:
        title = title[:59].rstrip() + "…"
    prompts.append({"name": title, "text": text})
    save_prompts(prompts)
    return prompts


def delete_prompt(index: int) -> list[dict]:
    prompts = load_prompts()
    if 0 <= index < len(prompts):
        prompts.pop(index)
        save_prompts(prompts)
    return prompts
