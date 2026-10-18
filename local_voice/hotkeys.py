"""Native recording shortcut registration; observes no other keyboard input."""
from __future__ import annotations

import ctypes
import re
import sys
import threading

SHORTCUT = "Shift+Right Arrow"


def _fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


class _EventType(ctypes.Structure):
    _fields_ = [("event_class", ctypes.c_uint32), ("kind", ctypes.c_uint32)]


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_SIGNATURE = _fourcc("LVce")
_NOT_HANDLED = -9874


def _carbon_api():
    api = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    definitions = {
        "GetApplicationEventTarget": (ctypes.c_void_p, []),
        "InstallEventHandler": (ctypes.c_int32, [ctypes.c_void_p, _HANDLER, ctypes.c_ulong,
                                                ctypes.POINTER(_EventType), ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_void_p)]),
        "RegisterEventHotKey": (ctypes.c_int32, [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID,
                                                ctypes.c_void_p, ctypes.c_uint32,
                                                ctypes.POINTER(ctypes.c_void_p)]),
        "GetEventKind": (ctypes.c_uint32, [ctypes.c_void_p]),
        "GetEventParameter": (ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                               ctypes.POINTER(ctypes.c_uint32), ctypes.c_ulong,
                                               ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]),
        "UnregisterEventHotKey": (ctypes.c_int32, [ctypes.c_void_p]),
        "RemoveEventHandler": (ctypes.c_int32, [ctypes.c_void_p]),
    }
    for name, (result, arguments) in definitions.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    return api


class MacGlobalHotkey:
    """Carbon delivers registered keys through the existing Cocoa main run loop."""
    def __init__(self, callback, api=None):
        self.callback = callback
        self.api = api if api is not None else _carbon_api()
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_ref = ctypes.c_void_p()
        self._handler = _HANDLER(self._dispatch)  # Keep the C callback alive until unregistered.
        self._down = False

    def start(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Register the macOS shortcut on the main thread")
        if self._hotkey_ref.value:
            return
        target = self.api.GetApplicationEventTarget()
        events = (_EventType * 2)(_EventType(_fourcc("keyb"), 5), _EventType(_fourcc("keyb"), 6))
        status = self.api.InstallEventHandler(target, self._handler, len(events), events, None,
                                             ctypes.byref(self._handler_ref))
        if status:
            raise RuntimeError(f"Could not install {SHORTCUT} handler (macOS {status})")
        # Physical Right Arrow (0x7C) is identical on Hebrew and English layouts.
        # Exclusive registration reports an existing binding instead of triggering two apps.
        status = self.api.RegisterEventHotKey(0x7C, 1 << 9, _HotKeyID(_SIGNATURE, 1),
                                             target, 1, ctypes.byref(self._hotkey_ref))
        if status:
            self.close()
            reason = "already in use" if status == -9878 else f"macOS error {status}"
            raise RuntimeError(f"{SHORTCUT} is unavailable ({reason})")

    def _dispatch(self, next_handler, event, user_data):
        if not self._hotkey_ref.value:
            return _NOT_HANDLED
        identity = _HotKeyID()
        status = self.api.GetEventParameter(event, _fourcc("----"), _fourcc("hkid"), None,
                                           ctypes.sizeof(identity), None, ctypes.byref(identity))
        if status or identity.signature != _SIGNATURE or identity.id != 1:
            return _NOT_HANDLED
        kind = self.api.GetEventKind(event)
        if kind == 6:
            self._down = False
        elif kind == 5:
            if not self._down:
                self._down = True
                try:
                    self.callback()
                except Exception as exc:
                    # Exceptions must not escape a ctypes callback into the OS event loop.
                    print(f"[Local Voice] Shortcut handler failed: {exc}")
        else:
            return _NOT_HANDLED
        return 0

    def close(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Unregister the macOS shortcut on the main thread")
        if self._hotkey_ref.value:
            self.api.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()
        if self._handler_ref.value:
            self.api.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()
        self._down = False


WINDOWS_SHORTCUT = "Ctrl+Alt+Space"


def windows_shortcut(value):
    """Validate a user-selected Windows binding without observing keyboard input."""
    parts = value.strip().upper().split("+")
    modifiers = {"CTRL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004}
    if len(parts) < 3 or len(set(parts)) != len(parts) or any(p not in modifiers for p in parts[:-1]):
        raise ValueError("Use Ctrl+Alt or Ctrl+Shift plus a letter, number, Space, or F1–F11.")
    if "CTRL" not in parts[:-1] or not ({"ALT", "SHIFT"} & set(parts[:-1])):
        raise ValueError("Include Ctrl and either Alt or Shift to avoid ordinary typing shortcuts.")
    key = parts[-1]
    if key == "SPACE":
        code = 0x20
    elif re.fullmatch(r"[A-Z0-9]", key):
        code = ord(key)
    elif re.fullmatch(r"F(?:[1-9]|10|11)", key):
        code = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError("Choose a letter, number, Space, or F1–F11. Windows-reserved keys are excluded.")
    return sum(modifiers[p] for p in parts[:-1]) | 0x4000, code


class WindowsGlobalHotkey:
    """Windows owns a small message pump; actions are queued onto the Tk UI thread."""
    def __init__(self, callback, shortcut=WINDOWS_SHORTCUT, *, api=None, startup_timeout=3):
        self.callback = callback
        self.shortcut = shortcut
        self.modifiers, self.key_code = windows_shortcut(shortcut)
        self.api = api
        self.startup_timeout = startup_timeout
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    def start(self):
        if self._thread is not None:
            if self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("The previous shortcut handler is still stopping.")
                return
            self._thread = None
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=self.startup_timeout):
            self._stop.set()
            self._thread.join(timeout=self.startup_timeout)
            if not self._thread.is_alive():
                self._thread = None
            raise RuntimeError(f"Could not initialize {self.shortcut}; initialization was cancelled.")
        if self._error is not None:
            self._thread.join()
            self._thread = None
            raise RuntimeError(self._error)

    def _run(self):
        registered = False
        try:
            from ctypes import wintypes

            user32 = self.api if self.api is not None else ctypes.WinDLL("user32", use_last_error=True)
            if self._stop.is_set():
                return
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                           wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0x0400, 0x0400, 0)
            if self._stop.is_set():
                return
            # MOD_NOREPEAT ignores held keys; this shortcut is a toggle.
            registered = bool(user32.RegisterHotKey(None, 0x4C56, self.modifiers, self.key_code))
            if not registered:
                self._error = f"{self.shortcut} is unavailable (Windows error {ctypes.get_last_error()})"
                return
            self._ready.set()
            # A stop event is independent of the native message queue. A failed
            # PostThreadMessage can no longer strand a registered hotkey.
            while not self._stop.is_set():
                while not self._stop.is_set() and user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    if message.message == 0x0312 and message.wParam == 0x4C56:
                        self.callback()
                self._stop.wait(0.03)
        except Exception as exc:
            self._error = str(exc)
        finally:
            if registered:
                if not user32.UnregisterHotKey(None, 0x4C56):
                    self._error = "Windows reported a shortcut release error; the owning thread is exiting."
            self._ready.set()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            if self._thread is threading.current_thread():
                return
            self._thread.join(timeout=self.startup_timeout)
            if self._thread.is_alive():
                raise RuntimeError("Shortcut cleanup is still in progress; its thread remains owned.")
            self._thread = None


def global_hotkey(callback, shortcut=WINDOWS_SHORTCUT):
    if sys.platform == "darwin":
        return MacGlobalHotkey(callback)
    if sys.platform == "win32":
        return WindowsGlobalHotkey(callback, shortcut)
    raise RuntimeError("Global shortcuts are supported on macOS and Windows")
