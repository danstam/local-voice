from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Local Voice"
LEGACY_APP_NAMES = ("Local Dictation Island",)


def _app_data_dir_for_name(app_name: str) -> Path:
    home = Path.home()

    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / app_name

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / app_name
        return home / "AppData" / "Local" / app_name

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / app_name
    return home / ".local" / "share" / app_name


def app_data_dir() -> Path:
    target = _app_data_dir_for_name(APP_NAME)
    if target.exists():
        return target

    for legacy_name in LEGACY_APP_NAMES:
        legacy = _app_data_dir_for_name(legacy_name)
        if not legacy.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            legacy.rename(target)
            return target
        except OSError:
            return legacy

    return target
