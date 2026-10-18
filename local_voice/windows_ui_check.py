"""Exercise real Tk widgets without recording, downloading, or registering shortcuts.

Run with the installed runtime via ``windows_entry.py --setup --ui-check`` on
Windows. This also runs on macOS for shared layout checks, not Windows certification.
"""
from contextlib import ExitStack
import json
import sys
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch


def _require(condition, message):
    if not condition:
        raise RuntimeError("Windows UI check failed: " + message)


def _check_layout(widget):
    """Controls must fit their containers and retain their requested text space."""
    for child in widget.winfo_children():
        if not child.winfo_ismapped() or isinstance(child, (tk.Menu, tk.Toplevel)):
            continue
        if isinstance(child, (tk.Label, tk.Button, ttk.Combobox, ttk.Button, ttk.Entry, ttk.Checkbutton)):
            title = child.cget("text") if "text" in child.keys() else child.winfo_class()
            _require(child.winfo_width() >= child.winfo_reqwidth(), f"clipped width: {title!r}")
            _require(child.winfo_height() >= child.winfo_reqheight(), f"clipped height: {title!r}")
        _require(child.winfo_x() >= 0 and child.winfo_y() >= 0, f"negative bounds: {child}")
        _require(child.winfo_x() + child.winfo_width() <= widget.winfo_width(), f"outside right edge: {child}")
        _require(child.winfo_y() + child.winfo_height() <= widget.winfo_height(), f"outside bottom edge: {child}")
        _check_layout(child)


def check_ui():
    from . import app_windows as ui
    from . import audio_devices

    reports = []
    # Tk scaling is pixels/point: 96, 144, and 192 DPI correspond to 100/150/200%.
    for dpi in (96, 144, 192):
        root = tk.Tk()
        try:
            root.tk.call("tk", "scaling", dpi / 72)
            with ExitStack() as stack:
                stack.enter_context(patch.object(ui, "cleanup_stale_workspaces"))
                stack.enter_context(patch.object(ui, "read_settings", return_value={}))
                save = stack.enter_context(patch.object(ui, "write_settings"))
                microphone = stack.enter_context(patch.object(ui, "Recorder"))
                shortcut = stack.enter_context(patch.object(ui, "global_hotkey"))
                stack.enter_context(patch.object(ui.WhisperTranscriber, "_load_settings", return_value={"model": "medium"}))
                stack.enter_context(patch.object(ui.WhisperTranscriber, "_write_settings"))
                stack.enter_context(patch.object(ui.DictationApp, "_start_model_warmup"))
                stack.enter_context(patch.object(audio_devices, "input_devices", return_value=[
                    {"name": "Test microphone", "hostapi": "WASAPI", "label": "Test microphone (WASAPI)"}]))
                app = ui.DictationApp(root)
                app.model_ready = True
                app._set_loading(False)
                app._set_status("Ready", ui.GREEN)
                app._refresh_buttons()
                root.update()
                _require(app.expanded_view.winfo_ismapped() and bool(root.cget("menu")), "expanded startup and menu")
                _require(not hasattr(app, "compact_view"), "Windows compact mode must stay unavailable")
                _require(app.record_button.frame.winfo_ismapped() and app.stop_button.frame.winfo_ismapped(),
                         "recording controls must be visible without a hotkey")
                _check_layout(app.expanded_view)
                expanded_size = [root.winfo_width(), root.winfo_height()]

                for model in ("small", "turbo", "medium", "large-v3"):
                    app.transcriber.set_model_name(model)
                    app._refresh_buttons()
                    _require(app.english_button.enabled == (model in ("medium", "large-v3")), "English model compatibility")
                app._invoke_button(app.english_button)
                _require(app.transcriber.translate_to_english_enabled(), "English toggle")

                for recording, paused, transcribing, status in (
                    (True, False, False, "Recording"), (False, True, False, "Paused"),
                    (False, False, True, "Working"), (False, False, False, "Ready")
                ):
                    app.recording, app.paused, app.transcribing = recording, paused, transcribing
                    app._set_status(status, ui.GREEN)
                    app._refresh_buttons()
                    root.update()
                    _require(not app.english_button.enabled if recording or paused or transcribing else app.english_button.enabled,
                             "English busy-state control")
                    _check_layout(app.expanded_view)
                app._set_transcript("שלום עולם\nEnglish translation stays on this computer.")
                _require(app._current_text().startswith("שלום עולם\n"), "multilingual transcript")
                app.open_settings()
                root.update()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                _check_layout(dialog)
                dialog.destroy()
                shortcut.assert_not_called()
                save.assert_not_called()
                microphone.return_value.start.assert_not_called()
                reports.append({"dpi": dpi, "tk_scaling": float(root.tk.call("tk", "scaling")),
                                "expanded": expanded_size, "passed": True})
        finally:
            root.destroy()
    return {"platform": sys.platform, "native_windows": sys.platform == "win32", "layouts": reports}


if __name__ == "__main__":
    print(json.dumps(check_ui()))
