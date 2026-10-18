"""Graphical model setup for the self-contained Windows distribution."""
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox


def model_setup(parent=None, on_ready=None, on_cancel=None):
    from .transcriber import WhisperTranscriber, MODEL_TITLES
    from .windows_setup import MODELS, probe, system_memory, memory_warning
    from .windows_runtime import console_python
    dialog = tk.Tk() if parent is None else tk.Toplevel(parent)
    dialog.title("Local Voice setup")
    dialog.resizable(False, False)
    body = ttk.Frame(dialog, padding=20)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Choose a local transcription model", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(body, text="One download is needed. Speech and recordings stay on this computer.").pack(anchor="w", pady=(8, 14))
    status = tk.StringVar(value="Checking this computer…")
    ttk.Label(body, textvariable=status, wraplength=440).pack(anchor="w")
    selected = tk.StringVar(value="small")
    selector = ttk.Combobox(body, state="readonly", textvariable=selected,
                             values=["small", "turbo", "medium", "large-v3"])
    selector.pack(fill="x", pady=14)
    ttk.Label(body, text="Small: lighter, less accurate. Medium/Large: translation to English.", wraplength=440).pack(anchor="w")
    progress = ttk.Progressbar(body, mode="indeterminate")
    progress.pack(fill="x", pady=14)
    events = queue.Queue()
    cancelled = threading.Event()
    state = {"busy": True, "ready": False, "runtime": None, "memory": None, "closing": False}
    if on_cancel:
        dialog.bind("<Destroy>", lambda event: on_cancel() if event.widget == dialog and not state["ready"] else None)

    def inspect():
        try:
            runtime = probe(console_python())
            events.put(("hardware", (runtime, system_memory())))
        except Exception as exc:
            events.put(("error", str(exc)))

    def install(model):
        transcriber = WhisperTranscriber()
        try:
            transcriber.download_model(model, progress_callback=lambda s: events.put(("status", s)), cancel=cancelled)
            transcriber.set_model_name(model)
            transcriber._device = state["runtime"]["device"]
            try:
                transcriber._load_model(progress_callback=lambda s: events.put(("status", s)), cancel=cancelled)
            except Exception:
                if cancelled.is_set() or transcriber._device != "cuda" or state["memory"]["available_gib"] < MODELS[model]["cpu"]:
                    raise
                transcriber.unload_model()
                transcriber._device = "cpu"
                events.put(("status", "GPU loading failed. Trying this model on CPU…"))
                transcriber._load_model(progress_callback=lambda s: events.put(("status", s)), cancel=cancelled)
            if not cancelled.is_set():
                transcriber._write_settings({"model": model, "windows_device": transcriber._device})
                events.put(("done", model))
        except Exception as exc:
            events.put(("error", str(exc)))
        finally:
            transcriber.unload_model()
            events.put(("finished", None))

    def begin():
        warning = memory_warning(selected.get(), state["runtime"], state["memory"])
        if warning and not messagebox.askyesno("Model may be too large", warning + "\nTry it anyway?", parent=dialog):
            return
        state["busy"] = True
        cancelled.clear()
        download_button.configure(state="disabled")
        selector.configure(state="disabled")
        progress.start()
        threading.Thread(target=install, args=(selected.get(),), daemon=True).start()

    def close():
        if state["busy"]:
            state["closing"] = True
            cancelled.set()
            status.set("Stopping setup safely…")
        else:
            dialog.destroy()

    download_button = ttk.Button(body, text="Download and start", command=begin, state="disabled")
    download_button.pack(side="right")
    ttk.Button(body, text="Cancel", command=close).pack(side="left")
    dialog.protocol("WM_DELETE_WINDOW", close)
    progress.start()

    def poll():
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == "hardware":
                state.update(runtime=payload[0], memory=payload[1], busy=False)
                recommended = "turbo" if payload[0]["device"] == "cuda" and not memory_warning("turbo", *payload) else "small"
                selected.set(recommended)
                status.set(f"Using {payload[0]['gpu'] or 'CPU'}. Available RAM: {payload[1]['available_gib']:.1f} GiB. Recommended: {MODEL_TITLES[recommended]}.")
                progress.stop()
                download_button.configure(state="normal")
                if state["closing"]:
                    dialog.destroy()
                    return
            elif kind == "status":
                status.set(payload)
            elif kind == "error":
                status.set(payload)
                state["busy"] = False
                progress.stop()
                download_button.configure(state="normal" if state["runtime"] else "disabled")
                selector.configure(state="readonly")
                if state["closing"]:
                    dialog.destroy()
                    return
            elif kind == "done":
                state.update(ready=True, busy=False, model=payload)
                dialog.destroy()
                if on_ready:
                    on_ready(payload)
                return
            elif kind == "finished" and state["closing"]:
                dialog.destroy()
                return
        dialog.after(100, poll)

    dialog.after(100, poll)
    threading.Thread(target=inspect, daemon=True).start()
    if parent is None:
        dialog.mainloop()
        return state.get("model") if state["ready"] else None
    dialog.transient(parent)
    return dialog


def main():
    from .paths import app_data_dir
    folder = app_data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = (folder / "windows-app.log").open("a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    from . import cli
    import argparse
    parser = argparse.ArgumentParser()
    cli.arguments(parser)
    args = parser.parse_args()
    if cli.handle(args):
        return
    from .transcriber import WhisperTranscriber
    transcriber = WhisperTranscriber()
    if not transcriber.is_model_cached(transcriber.current_model_name()):
        model = model_setup()
        if not model:
            return
        import os
        os.environ["LDI_MODEL"] = model
    from .app_windows import DictationApp
    root = tk.Tk()
    DictationApp(root)
    root.mainloop()
