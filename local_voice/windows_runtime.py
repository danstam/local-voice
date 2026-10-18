"""Helpers for an application-owned, relocatable Windows Python distribution."""
import json
from pathlib import Path
import sys
import uuid

PROJECT = Path(__file__).resolve().parent.parent


def command(python, mode, *arguments):
    return [str(python), "-I", "-X", "utf8", "-u", str(PROJECT / "windows_entry.py"), mode, *map(str, arguments)]


def active_python(project=PROJECT):
    base = project / ".windows"
    try:
        name = (base / "active-runtime.txt").read_text(encoding="utf-8").strip()
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
            return None
        target = base / "runtimes" / name / "python" / "python.exe"
        return target if target.is_file() else None
    except OSError:
        return None


def activate_runtime(path, project=PROJECT):
    base = project / ".windows"
    # Only a completed version inside this app may become active.
    root = path.resolve().parent.parent
    if root.parent != (base / "runtimes").resolve() or not (root / "runtime-ready.json").is_file():
        raise ValueError("Refusing to activate an unverified runtime.")
    temporary = base / ("active-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(root.name, encoding="utf-8")
    temporary.replace(base / "active-runtime.txt")


def console_python():
    path = Path(sys.executable)
    return path.with_name("python.exe") if path.name.lower() == "pythonw.exe" else path
