"""Install an owned, reversible `voice` command for the current user only."""
from dataclasses import dataclass
from contextlib import contextmanager
import errno
import hashlib
import json
import logging
import ntpath
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import time
import uuid

from .paths import app_data_dir

OWNER = "local-voice-command-v1"
BEGIN = "# >>> Local Voice command >>>"
END = "# <<< Local Voice command <<<"
REOPEN = "Close and reopen your terminal, then type voice from any folder."
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result:
    status: str
    message: str


@contextmanager
def _command_lock(path):
    # Setup itself runs in a bare bootstrap Python. Keep this helper stdlib-only.
    with path.open("a+b") as handle:
        if not path.stat().st_size:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another command setup is running. Retry when it finishes.") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write(path, data, mode=0o600):
    """Preserve symlinks, permissions, and unrelated file contents."""
    path = path.resolve() if path.is_symlink() else path
    if path.exists() and path.read_bytes() == data:
        return
    permissions = stat.S_IMODE(path.stat().st_mode) if path.exists() else mode
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.chmod(permissions)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _state(root):
    target = root / "command.json"
    if not target.exists():
        return None
    value = json.loads(target.read_text(encoding="utf-8"))
    if value.get("owner") != OWNER:
        raise RuntimeError("The command folder belongs to another tool.")
    return value


def _mac_profiles(home, environ):
    shell = Path(environ.get("SHELL", "/bin/zsh")).name
    if shell == "zsh":
        folder = Path(environ.get("ZDOTDIR") or home).expanduser()
        if not folder.is_absolute():
            raise RuntimeError("The shell configuration directory is not absolute.")
        return [folder / ".zshrc"], False
    if shell == "bash":
        login = next((home / name for name in (".bash_profile", ".bash_login", ".profile")
                      if (home / name).exists()), home / ".bash_profile")
        return [login, home / ".bashrc"], False
    if shell == "fish":
        folder = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
        if not folder.is_absolute():
            raise RuntimeError("The shell configuration directory is not absolute.")
        return [folder / "fish/conf.d/local-voice.fish"], True
    raise RuntimeError("This shell is not supported by automatic command installation.")


def _mac_block(bin_dir, fish=False):
    if ":" in str(bin_dir) or "\n" in str(bin_dir):
        raise RuntimeError("The command folder cannot be represented safely in PATH.")
    if fish:
        quoted = "'" + str(bin_dir).replace("\\", "\\\\").replace("'", "\\'") + "'"
        body = f"if not contains -- {quoted} $PATH\n    set -gx PATH $PATH {quoted}\nend\n"
    else:
        quoted = shlex.quote(str(bin_dir))
        body = (f"case \":${{PATH-}}:\" in\n  *:{quoted}:*) ;;\n"
                f"  *) export PATH=\"${{PATH:+$PATH:}}\"{quoted} ;;\nesac\n")
    return f"{BEGIN}\n{body}{END}\n".encode("utf-8")


def _existing_alias(home, environ):
    # Aliases/functions take precedence over PATH. Leave common definitions alone
    # without executing arbitrary shell startup scripts to inspect them.
    folder = Path(environ.get("ZDOTDIR") or home).expanduser()
    paths = {folder / ".zshrc", folder / ".zprofile", home / ".bashrc",
             home / ".bash_profile", home / ".bash_login", home / ".profile"}
    pattern = re.compile(rb"(?m)^\s*(?:alias\s+(?:--\s+)?voice\s*=|function\s+voice(?:\s|\(|\{)|voice\s*\(\s*\))")
    return any(path.is_file() and pattern.search(path.read_bytes()) for path in paths)


def _windows_entry(value, environ):
    variables = {k.lower(): v for k, v in environ.items()}
    expanded = re.sub(r"%([^%]+)%", lambda m: variables.get(m[1].lower(), m[0]), value.strip().strip('"'))
    return ntpath.normcase(ntpath.normpath(expanded))


def _append_windows_path(value, directory, environ):
    raw = value or ""
    if any(_windows_entry(item, environ) == _windows_entry(directory, environ) for item in raw.split(";") if item):
        return raw, False
    return raw + (";" if raw and not raw.endswith(";") else "") + directory, True


class WindowsUserPath:
    def __init__(self):
        import winreg
        self.registry = winreg

    def read(self):
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, "Environment") as key:
                return r.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return None, r.REG_EXPAND_SZ

    def write(self, value, kind):
        r = self.registry
        with r.CreateKeyEx(r.HKEY_CURRENT_USER, "Environment", 0, r.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    r.DeleteValue(key, "Path")
                except FileNotFoundError:
                    pass
            else:
                r.SetValueEx(key, "Path", 0, kind, value)

    def notify(self):
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        api.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t()
        return bool(api.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 2000, ctypes.byref(result)))


def _launcher_files(project, windows):
    if windows:
        return {
            "voice.cmd": ("@echo off\r\nrem " + OWNER + "\r\nsetlocal DisableDelayedExpansion\r\n"
                '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0voice.ps1" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n").encode("utf-8"),
            "voice.ps1": ("# " + OWNER + "\n$ErrorActionPreference = 'Stop'\ntry {\n"
                "    $Config = Get-Content -LiteralPath (Join-Path $PSScriptRoot '../command.json') -Raw -Encoding UTF8 | ConvertFrom-Json\n"
                "    $Entry = Join-Path $Config.project 'windows/launch.ps1'\n"
                "    if (-not (Test-Path -LiteralPath $Entry)) { throw 'Project folder moved or removed.' }\n"
                "    & $Entry @args\n    exit $LASTEXITCODE\n} catch {\n"
                "    Write-Host 'Local Voice could not open. Run ./setup.cmd from its project folder to repair the shortcut.'\n"
                "    exit 1\n}\n").encode("utf-8"),
        }
    return {"voice": ("#!/bin/sh\n# " + OWNER + "\nproject=" + shlex.quote(str(project)) + "\n"
        'if [ ! -f "$project/scripts/start-posix.sh" ]; then\n'
        "    echo 'Local Voice was moved or removed. Run ./setup.cmd from its new folder to repair the shortcut.' >&2\n"
        '    exit 1\nfi\nexec /bin/sh "$project/scripts/start-posix.sh" "$@"\n').encode("utf-8")}


def install_command(project, *, home=None, data=None, environ=None, platform=None, registry=None):
    """Injectable locations let checks run without touching real user settings."""
    project = Path(project).resolve()
    home = Path(home) if home is not None else Path.home()
    root = (Path(data) if data is not None else app_data_dir()) / "command"
    environ = dict(os.environ if environ is None else environ)
    platform = platform or sys.platform
    if platform not in {"darwin", "win32"}:
        return Result("unavailable", "Use ./start.cmd to open Local Voice on this system.")
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        with _command_lock(root.parent / ".command.lock"):
            return _install(project, home, root, environ, platform, registry)
    except Exception:
        log.exception("Could not install the user command; existing commands were preserved")
        return Result("unavailable", "The voice shortcut could not be added automatically. Use ./start.cmd; details are in the setup log.")


def _install(project, home, root, environ, platform, registry):
    windows = platform == "win32"
    bin_dir = root / "bin"
    state = _state(root)
    if state and state["project"] != str(project) and Path(state["project"]).is_dir():
        return Result("existing", "An existing voice command was kept. Use ./start.cmd to open this copy.")
    found = shutil.which("voice", path=environ.get("PATH", ""))
    launcher = bin_dir / ("voice.cmd" if windows else "voice")
    if ((found and Path(found).resolve() != launcher.resolve())
            or (not windows and _existing_alias(home, environ))):
        return Result("existing", "An existing voice command was kept. Use ./start.cmd to open this copy.")
    files = _launcher_files(project, windows)
    for name in files:
        target = bin_dir / name
        if target.exists():
            marker = ("rem " if name.endswith(".cmd") else "# ") + OWNER
            if marker.encode() not in target.read_bytes().splitlines()[:2]:
                raise RuntimeError("An unrelated command already occupies the launcher folder.")
            if state and name in state["files"] and hashlib.sha256(target.read_bytes()).hexdigest() != state["files"][name]:
                raise RuntimeError("The managed command was edited; leaving it unchanged.")
    profiles = list(state.get("profiles", [])) if state else []
    changes = []
    old_path = None
    if windows:
        registry = registry or WindowsUserPath()
        old_path = registry.read()
        new_path, added = _append_windows_path(old_path[0], str(bin_dir), environ)
        path_added = added or bool(state and state.get("path_added"))
    else:
        paths, fish = _mac_profiles(home, environ)
        block = _mac_block(bin_dir, fish)
        for path in paths:
            path = path.resolve() if path.is_symlink() else path
            original = path.read_bytes() if path.exists() else None
            if original is not None and (BEGIN.encode() in original or END.encode() in original) and block not in original:
                raise RuntimeError("The existing command configuration was edited; leaving it unchanged.")
            if block in (original or b""):
                if not any(item["path"] == str(path) for item in profiles):
                    profiles.append(dict(path=str(path), block=block.decode(), separator=False, created=False))
                continue
            separator = bool(original and not original.endswith(b"\n"))
            content = (original or b"") + (b"\n" if separator else b"") + block
            changes.append((path, original, content))
            profiles.append(dict(path=str(path), block=block.decode(), separator=separator, created=original is None))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = dict(owner=OWNER, project=str(project), platform=platform, profiles=profiles,
                  files={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    if windows:
        record.update(path_added=path_added, path_created=(state.get("path_created", False) if state else old_path[0] is None))
        for key in ("path_before", "path_before_type", "path_after"):
            if state and key in state:
                record[key] = state[key]
        if added:
            record.update(path_before=old_path[0], path_before_type=old_path[1], path_after=new_path)
    previous_state = (root / "command.json").read_bytes() if state else None
    previous_files = {name: (bin_dir / name).read_bytes() if (bin_dir / name).exists() else None for name in files}
    try:
        for name, content in files.items():
            _write(bin_dir / name, content, 0o755)
        # Recovery metadata is saved before editing a profile or PATH.
        _write(root / "command.json", json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
        for path, original, content in changes:
            if original is not None:
                backup = root / "profile-backups" / (hashlib.sha256(str(path).encode()).hexdigest() + ".bak")
                if not backup.exists():
                    _write(backup, original)
            if (path.read_bytes() if path.exists() else None) != original:
                raise RuntimeError("Shell configuration changed during setup; no concurrent edits were overwritten.")
            _write(path, content)
        if windows and added:
            if registry.read() != old_path:
                raise RuntimeError("PATH changed during setup; no concurrent edits were overwritten.")
            registry.write(new_path, old_path[1])
    except Exception:
        # Roll back only bytes still equal to our writes, preserving concurrent edits.
        for path, original, content in reversed(changes):
            if path.exists() and path.read_bytes() == content:
                _write(path, original) if original is not None else path.unlink()
        for name, original in previous_files.items():
            target = bin_dir / name
            _write(target, original, 0o755) if original is not None else target.unlink(missing_ok=True)
        _write(root / "command.json", previous_state) if previous_state is not None else (root / "command.json").unlink(missing_ok=True)
        raise
    if windows:
        try:
            if not registry.notify():
                return Result("installed", "The voice shortcut is installed. Sign out and back in to make it available in new terminals.")
        except Exception:
            log.exception("Could not notify Windows of the updated user PATH")
            return Result("installed", "The voice shortcut is installed. Sign out and back in to make it available in new terminals.")
    return Result("installed", REOPEN)


def remove_command(project, *, data=None, environ=None, registry=None):
    project = Path(project).resolve()
    root = (Path(data) if data is not None else app_data_dir()) / "command"
    try:
        if not root.exists():
            return Result("absent", "No Local Voice command shortcut is installed.")
        with _command_lock(root.parent / ".command.lock"):
            state = _state(root)
            if not state or state["project"] != str(project):
                return Result("existing", "The command belongs to another installation and was kept.")
            for name, digest in state["files"].items():
                path = root / "bin" / name
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("The launcher was edited; automatic removal was stopped.")
            changes = []
            for item in state.get("profiles", []):
                path = Path(item["path"])
                if not path.exists():
                    continue
                content = path.read_bytes()
                block = item["block"].encode("utf-8")
                if block not in content:
                    if BEGIN.encode() in content:
                        raise RuntimeError("The shortcut configuration was edited; automatic removal was stopped.")
                    continue
                before, after = content.split(block, 1)
                if item["separator"] and not after and before.endswith(b"\n"):
                    before = before[:-1]
                remaining = before + after
                changes.append((path, content, remaining, item["created"]))
            for path, original, remaining, created in changes:
                if not path.exists() or path.read_bytes() != original:
                    raise RuntimeError("Shell configuration changed during removal; no concurrent edits were overwritten.")
                _write(path, remaining) if remaining or not created or path.is_symlink() else path.unlink()
            if state["platform"] == "win32" and state.get("path_added"):
                registry = registry or WindowsUserPath()
                old, kind = registry.read()
                env = dict(os.environ if environ is None else environ)
                directory = _windows_entry(str(root / "bin"), env)
                parts = (old or "").split(";")
                for index, part in enumerate(parts):
                    if _windows_entry(part, env) == directory:
                        parts.pop(index)
                        break
                updated = ";".join(parts)
                if updated != (old or ""):
                    if registry.read() != (old, kind):
                        raise RuntimeError("PATH changed during removal; no concurrent edits were overwritten.")
                    if old == state.get("path_after") and kind == state.get("path_before_type"):
                        registry.write(state["path_before"], kind)
                    else:
                        registry.write(None if not updated and state.get("path_created") else updated, kind)
                try:
                    registry.notify()
                except Exception:
                    log.exception("Could not notify Windows of shortcut removal")
            for name, digest in state["files"].items():
                path = root / "bin" / name
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    path.unlink()
            (root / "command.json").unlink()
            return Result("removed", "The voice shortcut was removed. Local Voice and its models were kept. Reopen your terminal to refresh it.")
    except Exception:
        log.exception("Could not remove the command shortcut")
        return Result("unavailable", "The shortcut could not be removed automatically. See the setup log for details.")


def finish_setup(project, *, check=False):
    from . import setup_ui
    if check:
        setup_ui.say("Checks passed.")
        setup_ui.say("Run ./start.cmd to open Local Voice.")
        return
    setup_ui.say("Adding the voice command for your user account...")
    result = install_command(project)
    setup_ui.say("Setup complete.")
    setup_ui.say(result.message)


def remove_with_output(project):
    from . import setup_ui
    result = remove_command(project)
    setup_ui.say(result.message)
    if result.status == "unavailable":
        raise SystemExit(1)
