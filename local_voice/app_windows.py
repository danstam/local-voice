from __future__ import annotations

import argparse
import math
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

from .recorder import Recorder
from .sessions import ChunkedRecordingSession
from .transcriber import MODEL_OPTIONS, MODEL_TITLES, WhisperTranscriber

BASE_WINDOW_WIDTH = 360
BASE_WINDOW_HEIGHT = 260
UI_SCALE = 0.78
WINDOW_MARGIN = 24
POLL_INTERVAL_MS = 100
VISUALIZER_INTERVAL_MS = 80
IDLE_TEXT = "Record, speak, stop."

CARD_BG = "#0f1621"
PANEL_BG = "#121d2a"
TEXT_BG = "#09111a"
TEXT_PRIMARY = "#edf3fb"
TEXT_SECONDARY = "#9cadc3"
BORDER = "#233346"
CONTROL_BG = "#182432"
CONTROL_BG_ALT = "#1d2a3a"
CONTROL_BORDER = "#32485f"
ACCENT = "#4cc3ff"
ACCENT_STRONG = "#2b74eb"
GREEN = "#2ecc71"
ORANGE = "#f4a742"
RED = "#ef4f4f"
PINK = "#ff6b8f"
DISABLED_BG = "#172230"
DISABLED_TEXT = "#5f7086"
SCROLLBAR_BG = "#1a2736"
SCROLLBAR_ACTIVE = "#2b3f56"
SCROLLBAR_TROUGH = "#0d1520"


def S(value: int) -> int:
    return max(1, round(value * UI_SCALE))


WINDOW_WIDTH = S(BASE_WINDOW_WIDTH)
WINDOW_HEIGHT = S(BASE_WINDOW_HEIGHT)
OUTER_PAD_X = S(12)
OUTER_PAD_Y = S(10)
CONTENT_PAD_Y = S(10)
BUTTON_GAP = S(8)
HEADER_GAP = S(10)
BUTTON_PAD_X = S(10)
BUTTON_PAD_Y = S(8)
TEXT_PAD_X = S(10)
TEXT_PAD_Y = S(10)
PANEL_PAD_X = S(18)
PANEL_PAD_Y = S(18)
PROGRESS_PAD_Y = S(20)
PROGRESS_WIDTH = S(210)
WAVE_WIDTH = S(300)
WAVE_HEIGHT = S(92)
WAVE_BAR_WIDTH = max(4, S(8))
WAVE_GAP = max(3, S(7))
WAVE_IDLE_HALF_HEIGHT = max(3, S(4))
TEXT_LINES = max(4, round(7 * UI_SCALE))
UI_FONT = max(9, S(10))
STATUS_DOT_SIZE = max(8, S(10))


@dataclass
class UiEvent:
    kind: str
    payload: str = ""


@dataclass
class FlatButton:
    frame: tk.Frame
    label: tk.Label
    command: Callable[[], None]
    role: str
    enabled: bool = True
    hover: bool = False

    def pack(self, *args, **kwargs) -> None:
        self.frame.pack(*args, **kwargs)


class DictationApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Local Voice")
        self.root.configure(bg=CARD_BG)
        self.root.resizable(False, False)
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass

        self.recorder = Recorder()
        self.transcriber = WhisperTranscriber()
        self.ui_queue: queue.Queue[UiEvent] = queue.Queue()
        self.recording_session: ChunkedRecordingSession | None = None
        self.recording = False
        self.paused = False
        self.transcribing = False
        self.switching_model = False
        self.loading = False
        self.model_ready = False
        self.wave_history: list[float] = []
        self.wave_items: list[int] = []
        self.status_text = tk.StringVar(value="Loading")
        self.model_var = tk.StringVar()
        self.english_button: FlatButton | None = None
        self._build_window()

        self._set_status("Loading", ORANGE)
        self._set_transcript(IDLE_TEXT)
        self._set_loading(True)
        self._refresh_buttons()
        self._position_window()
        self.root.protocol("WM_DELETE_WINDOW", self.quit_clicked)
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)
        self.root.after(VISUALIZER_INTERVAL_MS, self._refresh_visualizer)
        self._start_model_warmup()

    def _build_window(self) -> None:
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")

        outer = tk.Frame(
            self.root,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
            padx=OUTER_PAD_X,
            pady=OUTER_PAD_Y,
        )
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=CARD_BG)
        header.pack(fill="x")

        status_group = tk.Frame(header, bg=CARD_BG)
        status_group.pack(side="left")

        controls_group = tk.Frame(header, bg=CARD_BG)
        controls_group.pack(side="right")

        self.status_dot = tk.Canvas(
            status_group,
            width=STATUS_DOT_SIZE,
            height=STATUS_DOT_SIZE,
            bg=CARD_BG,
            highlightthickness=0,
            bd=0,
        )
        self.status_dot.pack(side="left", pady=(2, 0))
        self.status_dot_item = self.status_dot.create_oval(
            1,
            1,
            STATUS_DOT_SIZE - 1,
            STATUS_DOT_SIZE - 1,
            fill=ORANGE,
            outline="",
        )

        status_label = tk.Label(
            status_group,
            textvariable=self.status_text,
            bg=CARD_BG,
            fg=TEXT_SECONDARY,
            font=("TkDefaultFont", UI_FONT, "bold"),
        )
        status_label.pack(side="left", padx=(6, 10))

        self.ui_style = ttk.Style(self.root)
        try:
            self.ui_style.theme_use("clam")
        except tk.TclError:
            pass
        self.ui_style.configure(
            "Voice.TCombobox",
            fieldbackground=CONTROL_BG,
            background=CONTROL_BG,
            foreground=TEXT_PRIMARY,
            arrowcolor=TEXT_PRIMARY,
            bordercolor=CONTROL_BORDER,
            lightcolor=CONTROL_BORDER,
            darkcolor=CONTROL_BORDER,
            relief="flat",
            padding=(S(8), S(4), S(4), S(4)),
            font=("TkDefaultFont", UI_FONT, "bold"),
        )
        self.ui_style.map(
            "Voice.TCombobox",
            fieldbackground=[("readonly", CONTROL_BG), ("disabled", DISABLED_BG)],
            background=[("readonly", CONTROL_BG), ("disabled", DISABLED_BG)],
            foreground=[("readonly", TEXT_PRIMARY), ("disabled", DISABLED_TEXT)],
            arrowcolor=[("readonly", TEXT_PRIMARY), ("disabled", DISABLED_TEXT)],
            bordercolor=[("focus", "#4b83df"), ("readonly", CONTROL_BORDER), ("disabled", "#203041")],
            lightcolor=[("focus", "#4b83df"), ("readonly", CONTROL_BORDER), ("disabled", "#203041")],
            darkcolor=[("focus", "#4b83df"), ("readonly", CONTROL_BORDER), ("disabled", "#203041")],
        )
        self.root.option_add("*TCombobox*Listbox.background", TEXT_BG)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT_PRIMARY)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_STRONG)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT_PRIMARY)
        self.root.option_add("*TCombobox*Listbox.font", ("TkDefaultFont", UI_FONT))

        combo_shell = tk.Frame(
            controls_group,
            bg=CONTROL_BG,
            highlightbackground=CONTROL_BORDER,
            highlightthickness=1,
            bd=0,
        )
        combo_shell.pack(side="left")

        self.model_combo = ttk.Combobox(
            combo_shell,
            state="readonly",
            width=max(8, round(11 * UI_SCALE)),
            values=[MODEL_TITLES[name] for name in MODEL_OPTIONS],
            textvariable=self.model_var,
            style="Voice.TCombobox",
        )
        self.model_combo.pack(fill="x")
        self.model_combo.bind("<<ComboboxSelected>>", self.model_selected)

        self.english_button = self._make_button(
            controls_group,
            "EN",
            command=self.english_toggle_clicked,
            role="toggle_off",
            width=max(3, round(4 * UI_SCALE)),
        )
        self.english_button.pack(side="left", padx=(HEADER_GAP, 0))

        self.quit_button = self._make_button(
            controls_group,
            "Quit",
            self.quit_clicked,
            "ghost",
            width=max(4, round(6 * UI_SCALE)),
        )
        self.quit_button.pack(side="left", padx=(HEADER_GAP, 0))

        content = tk.Frame(outer, bg=CARD_BG)
        content.pack(fill="both", expand=True, pady=(CONTENT_PAD_Y, CONTENT_PAD_Y))

        self.transcript_container = tk.Frame(
            content,
            bg=TEXT_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )
        transcript_inner = tk.Frame(self.transcript_container, bg=TEXT_BG, bd=0)
        transcript_inner.pack(fill="both", expand=True)
        self.transcript_view = tk.Text(
            transcript_inner,
            wrap="word",
            height=TEXT_LINES,
            bg=TEXT_BG,
            fg=TEXT_PRIMARY,
            insertbackground=ACCENT,
            selectbackground="#214f99",
            selectforeground=TEXT_PRIMARY,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=TEXT_PAD_X,
            pady=TEXT_PAD_Y,
            font=("TkDefaultFont", UI_FONT),
        )
        self.transcript_scrollbar = tk.Scrollbar(
            transcript_inner,
            orient="vertical",
            command=self.transcript_view.yview,
            bg=SCROLLBAR_BG,
            activebackground=SCROLLBAR_ACTIVE,
            troughcolor=SCROLLBAR_TROUGH,
            relief="flat",
            bd=0,
            highlightthickness=0,
            width=max(10, S(12)),
        )
        self.transcript_view.configure(yscrollcommand=self.transcript_scrollbar.set)
        self.transcript_view.pack(side="left", fill="both", expand=True)
        self.transcript_scrollbar.pack(side="right", fill="y")
        self.transcript_view.configure(state="disabled")

        self.loading_container = tk.Frame(
            content,
            bg=PANEL_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
            padx=PANEL_PAD_X,
            pady=PANEL_PAD_Y,
        )
        self.ui_style.configure(
            "Island.Horizontal.TProgressbar",
            troughcolor="#0d1520",
            background=ACCENT,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        self.loading_bar = ttk.Progressbar(
            self.loading_container,
            mode="indeterminate",
            length=PROGRESS_WIDTH,
            style="Island.Horizontal.TProgressbar",
        )
        self.loading_bar.pack(pady=PROGRESS_PAD_Y)

        self.recording_container = tk.Frame(
            content,
            bg=PANEL_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
            padx=TEXT_PAD_X,
            pady=TEXT_PAD_Y,
        )
        self.wave_canvas = tk.Canvas(
            self.recording_container,
            width=WAVE_WIDTH,
            height=WAVE_HEIGHT,
            bg=PANEL_BG,
            highlightthickness=0,
            bd=0,
        )
        self.wave_canvas.pack(fill="both", expand=True)
        self._build_waveform()

        self.transcript_container.pack(fill="both", expand=True)

        buttons = tk.Frame(outer, bg=CARD_BG)
        buttons.pack(fill="x")

        self.record_button = self._make_button(buttons, "Rec", self.record_clicked, "record")
        self.record_button.pack(side="left", fill="x", expand=True)

        self.stop_button = self._make_button(buttons, "Stop", self.stop_clicked, "stop")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=BUTTON_GAP)

        self.copy_button = self._make_button(buttons, "Copy", self.copy_clicked, "secondary")
        self.copy_button.pack(side="left", fill="x", expand=True)

    def _build_waveform(self) -> None:
        self.wave_items = []
        self.wave_history = [0.0] * 19
        bar_count = len(self.wave_history)
        width = WAVE_WIDTH
        height = WAVE_HEIGHT
        bar_width = WAVE_BAR_WIDTH
        gap = WAVE_GAP
        total_width = (bar_count * bar_width) + ((bar_count - 1) * gap)
        start_x = (width - total_width) / 2.0
        center_y = height / 2.0

        for index in range(bar_count):
            x1 = start_x + (index * (bar_width + gap))
            x2 = x1 + bar_width
            item = self.wave_canvas.create_rectangle(
                x1,
                center_y - WAVE_IDLE_HALF_HEIGHT,
                x2,
                center_y + WAVE_IDLE_HALF_HEIGHT,
                fill=ACCENT,
                outline="",
            )
            self.wave_items.append(item)

    def _make_button(
        self,
        parent: tk.Misc,
        title: str,
        command,
        role: str,
        *,
        width: int | None = None,
    ) -> FlatButton:
        frame = tk.Frame(
            parent,
            bg=DISABLED_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )
        label = tk.Label(
            frame,
            text=title,
            bg=DISABLED_BG,
            fg=TEXT_PRIMARY,
            padx=BUTTON_PAD_X,
            pady=BUTTON_PAD_Y,
            font=("TkDefaultFont", UI_FONT, "bold"),
            width=width,
        )
        label.pack(fill="both", expand=True)

        button = FlatButton(frame=frame, label=label, command=command, role=role)
        for widget in (button.frame, button.label):
            widget.bind("<Button-1>", lambda _event, btn=button: self._invoke_button(btn))
            widget.bind("<Enter>", lambda _event, btn=button: self._set_button_hover(btn, True))
            widget.bind("<Leave>", lambda _event, btn=button: self._set_button_hover(btn, False))
        self._style_button(button, title, role, True)
        return button

    def _invoke_button(self, button: FlatButton) -> None:
        if button.enabled:
            button.command()

    def _set_button_hover(self, button: FlatButton, hover: bool) -> None:
        button.hover = hover
        self._style_button(button, button.label.cget("text"), button.role, button.enabled)

    def _position_window(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = screen_width - WINDOW_WIDTH - WINDOW_MARGIN
        y = max(WINDOW_MARGIN, (screen_height // 2) - (WINDOW_HEIGHT // 2))
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")

    def record_clicked(self) -> None:
        if self.transcribing or self.switching_model:
            return

        if self.recording:
            try:
                self.recorder.pause()
            except Exception as exc:
                self._set_status("Mic error", PINK)
                self._set_transcript(str(exc))
                self._set_loading(False)
                return

            self.recording = False
            self.paused = True
            self._set_status("Paused", ORANGE)
            self._set_transcript("Recording paused. Press Resume or Stop.")
            self._refresh_buttons()
            return

        if self.paused:
            try:
                self.recorder.resume()
            except Exception as exc:
                self._set_status("Mic error", PINK)
                self._set_transcript(str(exc))
                self._set_loading(False)
                return

            self.recording = True
            self.paused = False
            self._set_status("Recording", RED)
            self._set_transcript("")
            self._refresh_buttons()
            return

        try:
            self.recorder.start()
        except Exception as exc:
            self._set_status("Mic error", PINK)
            self._set_transcript(str(exc))
            return

        self.recording_session = ChunkedRecordingSession(self.recorder, self.transcriber)
        self.recording = True
        self.paused = False
        self._set_status("Recording", RED)
        self._set_loading(False)
        self._set_transcript("")
        self._refresh_buttons()

    def stop_clicked(self) -> None:
        if (not self.recording and not self.paused) or self.recording_session is None:
            return

        session = self.recording_session
        self.recording = False
        self.paused = False
        self.transcribing = True
        self.recording_session = None
        self._set_status("Working", ORANGE)
        self._set_loading(True, "Transcribing...")
        self._refresh_buttons()
        thread = threading.Thread(target=self._finalize_recording_session, args=(session,), daemon=True)
        thread.start()

    def copy_clicked(self) -> None:
        text = self._current_text()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Copied", GREEN)

    def cancel_clicked(self) -> None:
        if (not self.recording and not self.paused) or self.recording_session is None:
            return

        session = self.recording_session
        self.recording_session = None
        self.recording = False
        self.paused = False
        try:
            self.recorder.stop()
        except Exception:
            pass
        self.recorder.clear()
        session.stop_requested.set()
        self._set_status("Ready", GREEN)
        self._ensure_idle_text()
        self._refresh_buttons()

    def quit_clicked(self) -> None:
        if self.recording or self.paused:
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.recorder.clear()
        self.root.destroy()

    def english_toggle_clicked(self) -> None:
        if not self._translation_toggle_enabled():
            return

        self.transcriber.save_translate_preference(not self.transcriber.translate_to_english_enabled())
        self._refresh_buttons()

    def model_selected(self, event) -> None:
        title = self.model_var.get()
        selected = None
        for model_name, model_title in MODEL_TITLES.items():
            if model_title == title:
                selected = model_name
                break

        if selected is not None:
            self._begin_model_switch(selected)

    def _drain_queue(self) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event.kind == "transcript":
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload or "No speech recognized.")
                self._set_status("Ready", GREEN)
                self._refresh_buttons()
            elif event.kind == "ready":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", GREEN)
                self._refresh_buttons()
            elif event.kind == "model_switched":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", GREEN)
                self._refresh_buttons()
            elif event.kind == "error":
                self.switching_model = False
                self.recording = False
                self.paused = False
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload)
                self._set_status("Failed", PINK)
                self._refresh_buttons()
            elif event.kind == "no_audio":
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload)
                self._set_status("No audio", ORANGE)
                self._refresh_buttons()
            elif event.kind == "progress":
                self._set_loading(True)

        self._schedule(POLL_INTERVAL_MS, self._drain_queue)

    def _refresh_visualizer(self) -> None:
        if self.recording:
            level = self.recorder.current_level()
            previous = self.wave_history[-1] if self.wave_history else 0.0
            smoothed = (level * 0.24) + (previous * 0.76)
            self.wave_history = self.wave_history[1:] + [smoothed]
            self._render_waveform()
        else:
            self._reset_waveform()

        self._schedule(VISUALIZER_INTERVAL_MS, self._refresh_visualizer)

    def _finalize_recording_session(self, session: ChunkedRecordingSession) -> None:
        try:
            text = session.stop_recording_and_wait(
                progress_callback=lambda message: self.ui_queue.put(UiEvent("progress", message))
            )
            if text is None:
                self.ui_queue.put(UiEvent("no_audio", "No audio was captured. Try again."))
                return
            if not text:
                text = "No speech recognized."
            self.ui_queue.put(UiEvent("transcript", text))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    def _warm_model_in_background(self) -> None:
        try:
            self.transcriber._load_model(progress_callback=lambda message: self.ui_queue.put(UiEvent("progress", message)))
            self.ui_queue.put(UiEvent("ready"))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    def _start_model_warmup(self) -> None:
        threading.Thread(target=self._warm_model_in_background, daemon=True).start()

    def _begin_model_switch(self, model_name: str) -> None:
        if self.recording or self.paused or self.transcribing or self.switching_model:
            self._refresh_model_selector()
            return
        if model_name == self.transcriber.current_model_name():
            self._refresh_model_selector()
            return
        if not self.transcriber.is_model_cached(model_name):
            self._set_status("Download first", ORANGE)
            self._set_transcript(
                f"{model_name} is not cached yet.\n\nRun:\n.\\voice_windows.bat --download-model {model_name}"
            )
            self._refresh_buttons()
            return

        self.switching_model = True
        self.model_ready = False
        self._set_status("Loading", ORANGE)
        self._set_loading(True)
        self._refresh_buttons()
        threading.Thread(target=self._switch_model_in_background, args=(model_name,), daemon=True).start()

    def _switch_model_in_background(self, model_name: str) -> None:
        try:
            self.transcriber.switch_model(
                model_name,
                progress_callback=lambda message: self.ui_queue.put(UiEvent("progress", message)),
            )
            self.transcriber.save_model_preference(model_name)
            self.ui_queue.put(UiEvent("model_switched"))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    def _set_status(self, text: str, color: str) -> None:
        self.status_text.set(text)
        self.status_dot.itemconfigure(self.status_dot_item, fill=color)

    def _set_transcript(self, text: str) -> None:
        self.transcript_view.configure(state="normal")
        self.transcript_view.delete("1.0", "end")
        self.transcript_view.insert("1.0", text)
        self.transcript_view.configure(state="disabled")
        self.transcript_view.see("1.0")

    def _set_loading(self, active: bool, message: str | None = None) -> None:
        self.loading = active
        self._refresh_content_mode()

    def _ensure_idle_text(self) -> None:
        if not self._current_text():
            self._set_transcript(IDLE_TEXT)

    def _refresh_content_mode(self) -> None:
        show_recording = self.recording and not self.loading and not self.transcribing and not self.switching_model
        show_loading = self.loading and not show_recording

        self.transcript_container.pack_forget()
        self.loading_container.pack_forget()
        self.recording_container.pack_forget()

        if show_recording:
            self.recording_container.pack(fill="both", expand=True)
            self.loading_bar.stop()
            self._render_waveform()
            return

        if show_loading:
            self.loading_container.pack(fill="both", expand=True)
            self.loading_bar.start(12)
            self._reset_waveform()
            return

        self.transcript_container.pack(fill="both", expand=True)
        self.loading_bar.stop()
        self._reset_waveform()

    def _refresh_buttons(self) -> None:
        busy = self.transcribing or self.switching_model
        can_copy = bool(self._current_text()) and not self.recording and not self.paused and not busy
        record_title = "Resume" if self.paused else ("Pause" if self.recording else "Rec")
        record_role = "record" if self.paused or not self.recording else "secondary"
        self._style_button(self.record_button, record_title, record_role, self.model_ready and not busy)
        self._style_button(self.stop_button, "Stop", "stop", self.recording or self.paused)
        if self.recording or self.paused:
            self.copy_button.command = self.cancel_clicked
            self._style_button(self.copy_button, "Cancel", "secondary", True)
        else:
            self.copy_button.command = self.copy_clicked
            self._style_button(self.copy_button, "Copy", "secondary", can_copy)
        self._style_button(self.quit_button, "Quit", "ghost", True)
        self._refresh_model_selector()
        self._refresh_translation_toggle()
        self._refresh_content_mode()

    def _refresh_model_selector(self) -> None:
        current = self.transcriber.current_model_name()
        self.model_var.set(MODEL_TITLES[current])

        interactive = (
            self.model_ready
            and not self.switching_model
            and not self.recording
            and not self.paused
            and not self.transcribing
        )
        self.model_combo.configure(state="readonly" if interactive else "disabled")

    def _refresh_translation_toggle(self) -> None:
        if self.english_button is None:
            return

        enabled = self._translation_toggle_enabled()
        role = "toggle_on" if self.transcriber.translate_to_english_enabled() else "toggle_off"
        self._style_button(self.english_button, "EN", role, enabled)

    def _translation_toggle_enabled(self) -> bool:
        return (
            not self.recording
            and not self.paused
            and not self.transcribing
            and self.transcriber.current_model_supports_translation()
        )

    def _style_button(self, button: FlatButton, title: str, role: str, enabled: bool) -> None:
        button.role = role
        button.enabled = enabled
        fill = self._button_fill_color(role, enabled, button.hover)
        text_color = self._button_text_color(role, enabled)
        cursor = "hand2" if enabled else "arrow"

        button.frame.configure(bg=fill, highlightbackground=self._button_border_color(role, enabled, button.hover), cursor=cursor)
        button.label.configure(text=title, bg=fill, fg=text_color, cursor=cursor)

    def _button_fill_color(self, role: str, enabled: bool, hover: bool = False) -> str:
        if not enabled:
            return DISABLED_BG
        if role == "record":
            fill = ACCENT_STRONG
        elif role == "toggle_on":
            fill = ACCENT_STRONG
        elif role == "toggle_off":
            fill = CONTROL_BG
        elif role == "stop":
            fill = "#ba4545"
        elif role == "ghost":
            fill = CONTROL_BG_ALT
        else:
            fill = CONTROL_BG_ALT

        if hover:
            return self._mix_hex(fill, "#ffffff", 0.10)
        return fill

    def _button_border_color(self, role: str, enabled: bool, hover: bool) -> str:
        if not enabled:
            return "#203041"
        base = CONTROL_BORDER if role == "secondary" else BORDER
        if role == "record":
            base = "#5a96ff"
        if role == "toggle_on":
            base = "#5a96ff"
        if role == "toggle_off":
            base = CONTROL_BORDER
        if role == "stop":
            base = "#d66b6b"
        if role == "ghost":
            base = CONTROL_BORDER
        if hover:
            return self._mix_hex(base, "#ffffff", 0.16)
        return base

    def _button_text_color(self, role: str, enabled: bool) -> str:
        if not enabled:
            return DISABLED_TEXT
        return TEXT_PRIMARY

    def _current_text(self) -> str:
        text = self.transcript_view.get("1.0", "end-1c").strip()
        if text == IDLE_TEXT:
            return ""
        return text

    def _render_waveform(self) -> None:
        if not self.wave_items:
            return

        width = WAVE_WIDTH
        height = WAVE_HEIGHT
        center_y = height / 2.0
        bar_width = WAVE_BAR_WIDTH
        gap = WAVE_GAP
        total_width = (len(self.wave_items) * bar_width) + ((len(self.wave_items) - 1) * gap)
        start_x = (width - total_width) / 2.0

        baseline = max(6.0, 8.0 * UI_SCALE)
        amplitude = max(30.0, 52.0 * UI_SCALE)
        count = len(self.wave_items)

        for index, item in enumerate(self.wave_items):
            left = self.wave_history[max(0, index - 1)]
            center = self.wave_history[index]
            right = self.wave_history[min(count - 1, index + 1)]
            level = (left * 0.15) + (center * 0.7) + (right * 0.15)
            eased = math.pow(max(0.0, min(1.0, level)), 0.82)
            item_height = baseline + (amplitude * eased)
            x1 = start_x + (index * (bar_width + gap))
            x2 = x1 + bar_width
            self.wave_canvas.coords(item, x1, center_y - (item_height / 2.0), x2, center_y + (item_height / 2.0))

            alpha_mix = 0.35 + (0.65 * max(eased, 0.08))
            color = self._mix_hex("#22425f", ACCENT, alpha_mix)
            self.wave_canvas.itemconfigure(item, fill=color)

    def _reset_waveform(self) -> None:
        if not self.wave_items:
            return

        self.wave_history = [0.0] * len(self.wave_items)
        width = WAVE_WIDTH
        height = WAVE_HEIGHT
        center_y = height / 2.0
        bar_width = WAVE_BAR_WIDTH
        gap = WAVE_GAP
        total_width = (len(self.wave_items) * bar_width) + ((len(self.wave_items) - 1) * gap)
        start_x = (width - total_width) / 2.0

        for index, item in enumerate(self.wave_items):
            x1 = start_x + (index * (bar_width + gap))
            x2 = x1 + bar_width
            self.wave_canvas.coords(item, x1, center_y - WAVE_IDLE_HALF_HEIGHT, x2, center_y + WAVE_IDLE_HALF_HEIGHT)
            self.wave_canvas.itemconfigure(item, fill="#2b4057")

    def _mix_hex(self, start_hex: str, end_hex: str, amount: float) -> str:
        amount = max(0.0, min(1.0, amount))
        start = tuple(int(start_hex[index : index + 2], 16) for index in (1, 3, 5))
        end = tuple(int(end_hex[index : index + 2], 16) for index in (1, 3, 5))
        mixed = tuple(round(start[channel] + ((end[channel] - start[channel]) * amount)) for channel in range(3))
        return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"

    def _schedule(self, delay_ms: int, callback) -> None:
        try:
            if self.root.winfo_exists():
                self.root.after(delay_ms, callback)
        except tk.TclError:
            return


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--download-model", choices=MODEL_OPTIONS)
    args = parser.parse_args()

    if args.download_model:
        transcriber = WhisperTranscriber()
        path = transcriber.download_model(args.download_model, progress_callback=print)
        print(f"Ready: {path}")
        return

    root = tk.Tk()
    DictationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
