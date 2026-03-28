from __future__ import annotations

import argparse
import math
import queue
import threading
from dataclasses import dataclass

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSRoundedBezelStyle,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSMiniControlSize,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSProgressIndicator,
    NSProgressIndicatorSpinningStyle,
    NSScreen,
    NSSmallControlSize,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSObject, NSTimer

from .recorder import Recorder
from .sessions import ChunkedRecordingSession
from .transcriber import MODEL_OPTIONS, MODEL_TITLES, WhisperTranscriber

BASE_WINDOW_WIDTH = 248
BASE_WINDOW_HEIGHT = 138
UI_SCALE = 0.85
WINDOW_WIDTH = round(BASE_WINDOW_WIDTH * UI_SCALE)
WINDOW_HEIGHT = round(BASE_WINDOW_HEIGHT * UI_SCALE)
IDLE_TEXT = "Record, speak, stop."


@dataclass
class UiEvent:
    kind: str
    payload: str = ""


class AppDelegate(NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None

        self.recorder = Recorder()
        self.transcriber = WhisperTranscriber()
        self.ui_queue: queue.Queue[UiEvent] = queue.Queue()
        self.recordingSession: ChunkedRecordingSession | None = None
        self.recording = False
        self.paused = False
        self.transcribing = False
        self.switching_model = False
        self.loading = False
        self.model_ready = False
        self.panel = None
        self.recordButton = None
        self.stopButton = None
        self.copyButton = None
        self.quitButton = None
        self.englishToggleButton = None
        self.modelSelectorButton = None
        self.modelMenuView = None
        self.modelOptionButtons = {}
        self.statusLabel = None
        self.titleLabel = None
        self.transcriptScroll = None
        self.transcriptView = None
        self.loadingView = None
        self.loadingIndicator = None
        self.recordingView = None
        self.waveBars = []
        self.waveHistory = []
        self.visualTimer = None
        self.statusDot = None
        self.queueTimer = None
        return self

    def applicationDidFinishLaunching_(self, notification) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._build_window()
        self._set_status("Loading", self._orange())
        self._set_transcript(IDLE_TEXT)
        self._set_loading(True)
        self._refresh_buttons()
        self.panel.orderFrontRegardless()
        app.activateIgnoringOtherApps_(True)
        self.queueTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, self, "drainQueue:", None, True
        )
        self.visualTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, self, "refreshVisualizer:", None, True
        )
        self._start_model_warmup()

    def applicationShouldTerminateAfterLastWindowClosed_(self, app) -> bool:
        return True

    def recordClicked_(self, sender) -> None:
        if self.transcribing or self.switching_model:
            return

        if self.recording:
            try:
                self.recorder.pause()
            except Exception as exc:
                self._set_status("Mic error", self._pink())
                self._set_transcript(str(exc))
                self._set_loading(False)
                return

            self.recording = False
            self.paused = True
            self._set_status("Paused", self._orange())
            self._refresh_buttons()
            return

        if self.paused:
            try:
                self.recorder.resume()
            except Exception as exc:
                self._set_status("Mic error", self._pink())
                self._set_transcript(str(exc))
                self._set_loading(False)
                return

            self.recording = True
            self.paused = False
            self._set_status("Recording", self._red())
            self._refresh_buttons()
            return

        try:
            self.recorder.start()
        except Exception as exc:
            self._set_status("Mic error", self._pink())
            self._set_transcript(str(exc))
            return

        self.recordingSession = ChunkedRecordingSession(self.recorder, self.transcriber)
        self.recording = True
        self.paused = False
        self._hide_model_menu()
        self._set_status("Recording", self._red())
        self._set_loading(False)
        self._set_transcript("")
        self._refresh_buttons()

    def stopClicked_(self, sender) -> None:
        if (not self.recording and not self.paused) or self.recordingSession is None:
            return

        session = self.recordingSession
        self.recording = False
        self.paused = False
        self.transcribing = True
        self._set_status("Working", self._orange())
        self._set_loading(True)
        self._refresh_buttons()
        self.recordingSession = None
        thread = threading.Thread(target=self._finalize_recording_session, args=(session,), daemon=True)
        thread.start()

    def copyClicked_(self, sender) -> None:
        text = self._current_text()
        if not text:
            return
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, NSPasteboardTypeString)
        self._set_status("Copied", self._green())

    def quitClicked_(self, sender) -> None:
        NSApplication.sharedApplication().terminate_(None)

    def toggleEnglishClicked_(self, sender) -> None:
        if self.englishToggleButton is None or not self.englishToggleButton.isEnabled():
            return

        self.transcriber.save_translate_preference(not self.transcriber.translate_to_english_enabled())
        self._refresh_buttons()

    def toggleModelMenuClicked_(self, sender) -> None:
        if self.modelSelectorButton is None or self.modelMenuView is None:
            return
        if not self.modelSelectorButton.isEnabled():
            return

        self.modelMenuView.setHidden_(not self.modelMenuView.isHidden())
        self._refresh_model_selector()

    def useTurboClicked_(self, sender) -> None:
        self._select_model("turbo")

    def useLargeClicked_(self, sender) -> None:
        self._select_model("large-v3")

    def useMediumClicked_(self, sender) -> None:
        self._select_model("medium")

    def useSmallClicked_(self, sender) -> None:
        self._select_model("small")

    def drainQueue_(self, timer) -> None:
        while True:
            try:
                event = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event.kind == "transcript":
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload or "No speech recognized.")
                self._set_status("Ready", self._green())
                self._refresh_buttons()
            elif event.kind == "ready":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", self._green())
                self._refresh_buttons()
            elif event.kind == "model_switched":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", self._green())
                self._refresh_buttons()
            elif event.kind == "error":
                self.switching_model = False
                self.recording = False
                self.paused = False
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload)
                self._set_status("Failed", self._pink())
                self._refresh_buttons()
            elif event.kind == "no_audio":
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload)
                self._set_status("No audio", self._orange())
                self._refresh_buttons()
            elif event.kind == "progress":
                self._set_loading(True)
        self._refresh_content_mode()

    def refreshVisualizer_(self, timer) -> None:
        self._advance_waveform()

    @objc.python_method
    def _finalize_recording_session(self, session: ChunkedRecordingSession) -> None:
        try:
            text = session.stop_recording_and_wait(progress_callback=lambda _message: self.ui_queue.put(UiEvent("progress")))
            if text is None:
                self.ui_queue.put(UiEvent("no_audio", "No audio was captured. Try again."))
                return
            if not text:
                text = "No speech recognized."
            self.ui_queue.put(UiEvent("transcript", text))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    @objc.python_method
    def _warm_model_in_background(self) -> None:
        try:
            self.transcriber._load_model(progress_callback=None)
            self.ui_queue.put(UiEvent("ready"))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    @objc.python_method
    def _start_model_warmup(self) -> None:
        thread = threading.Thread(target=self._warm_model_in_background, daemon=True)
        thread.start()

    @objc.python_method
    def _begin_model_switch(self, model_name: str) -> None:
        self._hide_model_menu()
        if self.recording or self.paused or self.transcribing or self.switching_model:
            return
        if model_name == self.transcriber.current_model_name():
            return
        if not self.transcriber.is_model_cached(model_name):
            self._set_status("Download first", self._orange())
            self._set_transcript(
                f"{model_name} is not cached yet.\n\nRun:\n./voice --download-model {model_name}"
            )
            self._refresh_buttons()
            return

        self.switching_model = True
        self.model_ready = False
        self._set_status("Loading", self._orange())
        self._set_loading(True)
        self._refresh_buttons()
        thread = threading.Thread(target=self._switch_model_in_background, args=(model_name,), daemon=True)
        thread.start()

    @objc.python_method
    def _select_model(self, model_name: str) -> None:
        self._hide_model_menu()
        self._begin_model_switch(model_name)

    @objc.python_method
    def _hide_model_menu(self) -> None:
        if self.modelMenuView is not None:
            self.modelMenuView.setHidden_(True)

    @objc.python_method
    def _switch_model_in_background(self, model_name: str) -> None:
        try:
            self.transcriber.switch_model(model_name, progress_callback=None)
            self.transcriber.save_model_preference(model_name)
            self.ui_queue.put(UiEvent("model_switched"))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    @objc.python_method
    def _build_window(self) -> None:
        style_mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT),
            style_mask,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setHasShadow_(True)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT))
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(self._scaled(15.0))
        content.layer().setBackgroundColor_(self._card_background().CGColor())
        content.layer().setBorderWidth_(1.0)
        content.layer().setBorderColor_(self._border_color().CGColor())
        self.panel.setContentView_(content)

        self._position_window()
        self._build_header(content)
        self._build_transcript(content)
        self._build_model_picker(content)
        self._build_buttons(content)

    @objc.python_method
    def _build_header(self, content) -> None:
        self.statusDot = NSView.alloc().initWithFrame_(self._rect(10, BASE_WINDOW_HEIGHT - 23, 6, 6))
        self.statusDot.setWantsLayer_(True)
        self.statusDot.layer().setCornerRadius_(self._scaled(3.0))
        content.addSubview_(self.statusDot)

        self.statusLabel = self._make_label(self._rect(22, BASE_WINDOW_HEIGHT - 28, 118, 16), "", 9, False)
        self.statusLabel.setTextColor_(self._text_secondary())
        content.addSubview_(self.statusLabel)

        self.quitButton = self._make_button(
            self._rect(BASE_WINDOW_WIDTH - 28, BASE_WINDOW_HEIGHT - 31, 18, 18),
            "×",
            "quitClicked:",
            role="ghost",
            control_size=NSMiniControlSize,
            font_size=11.5,
        )
        content.addSubview_(self.quitButton)

    @objc.python_method
    def _build_model_picker(self, content) -> None:
        model_width = 74
        toggle_width = 34
        toggle_gap = 9
        quit_gap = 7
        quit_x = BASE_WINDOW_WIDTH - 28
        toggle_x = quit_x - quit_gap - toggle_width
        model_x = toggle_x - toggle_gap - model_width

        self.englishToggleButton = self._make_button(
            self._rect(toggle_x, BASE_WINDOW_HEIGHT - 30, toggle_width, 18),
            "EN",
            "toggleEnglishClicked:",
            role="toggle_off",
            control_size=NSMiniControlSize,
            font_size=8.8,
        )
        self.englishToggleButton.setBordered_(False)
        self.englishToggleButton.setWantsLayer_(True)
        self.englishToggleButton.layer().setCornerRadius_(self._scaled(5.0))
        self.englishToggleButton.layer().setBorderWidth_(1.0)
        content.addSubview_(self.englishToggleButton)

        self.modelSelectorButton = self._make_button(
            self._rect(model_x, BASE_WINDOW_HEIGHT - 30, model_width, 18),
            "",
            "toggleModelMenuClicked:",
            role="selector",
            control_size=NSMiniControlSize,
            font_size=9.5,
        )
        content.addSubview_(self.modelSelectorButton)

        self.modelMenuView = NSView.alloc().initWithFrame_(self._rect(model_x - 8, BASE_WINDOW_HEIGHT - 111, 82, 76))
        self.modelMenuView.setWantsLayer_(True)
        self.modelMenuView.layer().setCornerRadius_(self._scaled(9.0))
        self.modelMenuView.layer().setBackgroundColor_(self._transcript_background().CGColor())
        self.modelMenuView.layer().setBorderWidth_(1.0)
        self.modelMenuView.layer().setBorderColor_(self._border_color().CGColor())
        self.modelMenuView.setHidden_(True)
        content.addSubview_(self.modelMenuView)

        option_specs = [
            ("turbo", "useTurboClicked:", 56),
            ("large-v3", "useLargeClicked:", 38),
            ("medium", "useMediumClicked:", 20),
            ("small", "useSmallClicked:", 2),
        ]
        for model_name, action, y in option_specs:
            button = self._make_button(
                self._rect(4, y, 72, 16),
                MODEL_TITLES[model_name],
                action,
                role="menu",
                control_size=NSMiniControlSize,
                font_size=9.0,
            )
            self.modelMenuView.addSubview_(button)
            self.modelOptionButtons[model_name] = button

    @objc.python_method
    def _build_transcript(self, content) -> None:
        frame = self._rect(10, 38, BASE_WINDOW_WIDTH - 20, 58)
        frame_width = frame.size.width
        frame_height = frame.size.height
        self.transcriptScroll = NSScrollView.alloc().initWithFrame_(frame)
        self.transcriptScroll.setHasVerticalScroller_(True)
        self.transcriptScroll.setBorderType_(0)
        self.transcriptScroll.setDrawsBackground_(False)

        self.transcriptView = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, frame_width, frame_height))
        self.transcriptView.setEditable_(False)
        self.transcriptView.setSelectable_(True)
        self.transcriptView.setRichText_(False)
        self.transcriptView.setImportsGraphics_(False)
        self.transcriptView.setHorizontallyResizable_(False)
        self.transcriptView.setVerticallyResizable_(True)
        self.transcriptView.setFont_(NSFont.systemFontOfSize_(self._font_size(10.5)))
        self.transcriptView.setTextColor_(self._text_primary())
        self.transcriptView.setBackgroundColor_(self._transcript_background())
        self.transcriptView.textContainer().setContainerSize_((frame_width - self._scaled(10), 10_000_000.0))
        self.transcriptView.textContainer().setWidthTracksTextView_(True)

        self.transcriptScroll.setDocumentView_(self.transcriptView)
        content.addSubview_(self.transcriptScroll)

        self.loadingView = NSView.alloc().initWithFrame_(frame)
        self.loadingView.setWantsLayer_(True)
        self.loadingView.layer().setCornerRadius_(self._scaled(10.0))
        self.loadingView.layer().setBackgroundColor_(self._transcript_background().CGColor())
        self.loadingView.layer().setBorderWidth_(1.0)
        self.loadingView.layer().setBorderColor_(self._border_color().CGColor())
        self.loadingView.setHidden_(True)

        self.loadingIndicator = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(
                (frame_width / 2.0) - self._scaled(8),
                (frame_height / 2.0) - self._scaled(8),
                self._scaled(16),
                self._scaled(16),
            )
        )
        self.loadingIndicator.setStyle_(NSProgressIndicatorSpinningStyle)
        self.loadingIndicator.setControlSize_(NSSmallControlSize)
        self.loadingIndicator.setIndeterminate_(True)
        self.loadingIndicator.setDisplayedWhenStopped_(False)
        self.loadingView.addSubview_(self.loadingIndicator)
        content.addSubview_(self.loadingView)

        self.recordingView = NSView.alloc().initWithFrame_(frame)
        self.recordingView.setWantsLayer_(True)
        self.recordingView.layer().setCornerRadius_(self._scaled(10.0))
        self.recordingView.layer().setBackgroundColor_(self._transcript_background().CGColor())
        self.recordingView.layer().setBorderWidth_(1.0)
        self.recordingView.layer().setBorderColor_(self._border_color().CGColor())
        self.recordingView.setHidden_(True)
        content.addSubview_(self.recordingView)
        self._build_recording_wave(frame.size.width, frame.size.height)

    @objc.python_method
    def _build_recording_wave(self, width: float, height: float) -> None:
        self.waveBars = []
        self.waveHistory = []
        if self.recordingView is None:
            return

        bar_width = self._scaled(4.0)
        bar_count = 19
        target_width = min(width - self._scaled(28.0), self._scaled(184.0))
        if bar_count <= 1:
            bar_spacing = 0.0
        else:
            bar_spacing = max(self._scaled(2.5), (target_width - (bar_count * bar_width)) / (bar_count - 1))
        total_width = (bar_count * bar_width) + ((bar_count - 1) * bar_spacing)
        start_x = (width - total_width) / 2.0
        center_y = height / 2.0

        for index in range(bar_count):
            bar = NSView.alloc().initWithFrame_(
                NSMakeRect(
                    start_x + (index * (bar_width + bar_spacing)),
                    center_y - self._scaled(3.0),
                    bar_width,
                    self._scaled(6.0),
                )
            )
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(bar_width / 2.0)
            bar.layer().setBackgroundColor_(self._wave_color().CGColor())
            self.recordingView.addSubview_(bar)
            self.waveBars.append(bar)
            self.waveHistory.append(0.0)

    @objc.python_method
    def _build_buttons(self, content) -> None:
        self.recordButton = self._make_button(
            self._rect(10, 9, 72, 22),
            "Rec",
            "recordClicked:",
            role="record",
            font_size=9.5,
        )
        content.addSubview_(self.recordButton)

        self.stopButton = self._make_button(
            self._rect(88, 9, 72, 22),
            "Stop",
            "stopClicked:",
            role="stop",
            font_size=9.5,
        )
        content.addSubview_(self.stopButton)

        self.copyButton = self._make_button(
            self._rect(166, 9, 72, 22),
            "Copy",
            "copyClicked:",
            role="secondary",
            font_size=9.5,
        )
        content.addSubview_(self.copyButton)

    @objc.python_method
    def _make_label(self, frame, text: str, size: float, bold: bool) -> NSTextField:
        label = NSTextField.alloc().initWithFrame_(frame)
        label.setBezeled_(False)
        label.setBordered_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setStringValue_(text)
        scaled_size = self._font_size(size)
        label.setFont_(NSFont.boldSystemFontOfSize_(scaled_size) if bold else NSFont.systemFontOfSize_(scaled_size))
        label.setTextColor_(self._text_primary())
        return label

    @objc.python_method
    def _make_button(
        self,
        frame,
        title: str,
        action: str,
        role: str = "secondary",
        control_size=NSSmallControlSize,
        font_size: float = 10.5,
    ) -> NSButton:
        button = NSButton.alloc().initWithFrame_(frame)
        button.setTarget_(self)
        button.setAction_(action)
        button.setBezelStyle_(NSRoundedBezelStyle)
        button.setControlSize_(control_size)
        button.setFont_(NSFont.systemFontOfSize_(self._font_size(font_size)))
        button.setBordered_(True)
        self._style_button(button, title, role, True)
        return button

    @objc.python_method
    def _position_window(self) -> None:
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - WINDOW_WIDTH - self._scaled(12)
        y = visible.origin.y + (visible.size.height / 2.0) - (WINDOW_HEIGHT / 2.0)
        self.panel.setFrameOrigin_((x, y))

    @objc.python_method
    def _scaled(self, value: float) -> float:
        return value * UI_SCALE

    @objc.python_method
    def _rect(self, x: float, y: float, width: float, height: float):
        return NSMakeRect(self._scaled(x), self._scaled(y), self._scaled(width), self._scaled(height))

    @objc.python_method
    def _font_size(self, size: float) -> float:
        return max(8.0, self._scaled(size))

    @objc.python_method
    def _refresh_buttons(self) -> None:
        busy = self.transcribing or self.switching_model
        can_copy = bool(self._current_text()) and not self.recording and not self.paused and not busy
        record_title = "Resume" if self.paused else ("Pause" if self.recording else "Rec")
        record_role = "record" if self.paused or not self.recording else "secondary"
        self._style_button(self.recordButton, record_title, record_role, self.model_ready and not busy)
        self._style_button(self.stopButton, "Stop", "stop", self.recording or self.paused)
        self._style_button(self.copyButton, "Copy", "secondary", can_copy)
        self._style_button(self.quitButton, "×", "ghost", True)
        self._refresh_model_selector()
        self._refresh_translation_toggle()
        self._refresh_content_mode()

    @objc.python_method
    def _current_text(self) -> str:
        text = self.transcriptView.string().strip()
        if text == IDLE_TEXT:
            return ""
        return text

    @objc.python_method
    def _set_status(self, text: str, color) -> None:
        self.statusLabel.setStringValue_(text)
        self.statusLabel.setTextColor_(color)
        self.statusDot.layer().setBackgroundColor_(color.CGColor())

    @objc.python_method
    def _set_transcript(self, text: str) -> None:
        self.transcriptView.setString_(text)

    @objc.python_method
    def _set_loading(self, active: bool) -> None:
        self.loading = active
        if self.transcriptScroll is None or self.loadingView is None or self.loadingIndicator is None:
            return
        self._refresh_content_mode()

    @objc.python_method
    def _ensure_idle_text(self) -> None:
        if not self._current_text():
            self._set_transcript(IDLE_TEXT)

    @objc.python_method
    def _refresh_content_mode(self) -> None:
        if self.transcriptScroll is None or self.loadingView is None or self.recordingView is None:
            return

        show_recording = self.recording and not self.loading and not self.transcribing and not self.switching_model
        show_loading = self.loading and not show_recording

        self.recordingView.setHidden_(not show_recording)
        self.loadingView.setHidden_(not show_loading)
        self.transcriptScroll.setHidden_(show_recording or show_loading)

        if show_loading:
            self.loadingIndicator.startAnimation_(None)
        else:
            self.loadingIndicator.stopAnimation_(None)

        if show_recording:
            self._render_waveform()
        else:
            self._reset_waveform()

    @objc.python_method
    def _advance_waveform(self) -> None:
        if self.recordingView is None or not self.waveBars:
            return

        if self.recording and not self.recordingView.isHidden():
            level = self.recorder.current_level()
            previous = self.waveHistory[-1] if self.waveHistory else 0.0
            smoothed = (level * 0.24) + (previous * 0.76)
            self.waveHistory = self.waveHistory[1:] + [smoothed]
            self._render_waveform()

    @objc.python_method
    def _render_waveform(self) -> None:
        if self.recordingView is None or self.recordingView.isHidden() or not self.waveBars:
            return

        center_y = self.recordingView.frame().size.height / 2.0
        baseline = self._scaled(3.5)
        amplitude = self._scaled(30.0)
        count = len(self.waveBars)

        for index, bar in enumerate(self.waveBars):
            left = self.waveHistory[max(0, index - 1)]
            center = self.waveHistory[index]
            right = self.waveHistory[min(count - 1, index + 1)]
            level = (left * 0.15) + (center * 0.7) + (right * 0.15)
            eased = math.pow(max(0.0, min(1.0, level)), 0.82)
            height = baseline + (amplitude * eased)
            frame = bar.frame()
            bar.setFrame_(NSMakeRect(frame.origin.x, center_y - (height / 2.0), frame.size.width, height))
            alpha = 0.22 + (0.5 * max(eased, 0.08))
            edge_fade = 0.96 + (0.04 * (1.0 - abs(((index / max(1, count - 1)) * 2.0) - 1.0)))
            bar.setAlphaValue_(alpha * edge_fade)

    @objc.python_method
    def _reset_waveform(self) -> None:
        if self.recordingView is None or not self.waveBars:
            return

        self.waveHistory = [0.0] * len(self.waveBars)
        center_y = self.recordingView.frame().size.height / 2.0
        for bar in self.waveBars:
            frame = bar.frame()
            bar.setFrame_(NSMakeRect(frame.origin.x, center_y - self._scaled(2.0), frame.size.width, self._scaled(4.0)))
            bar.setAlphaValue_(0.28)

    @objc.python_method
    def _refresh_model_selector(self) -> None:
        if self.modelSelectorButton is None or self.modelMenuView is None:
            return

        current = self.transcriber.current_model_name()
        interactive = self.model_ready and not self.switching_model and not self.recording and not self.paused and not self.transcribing

        self._style_button(self.modelSelectorButton, f"{MODEL_TITLES[current]} ▾", "selector", interactive)

        if not interactive:
            self._hide_model_menu()

        for model_name, button in self.modelOptionButtons.items():
            active = current == model_name
            role = "active_menu" if active else "menu"
            enabled = interactive and not active
            title = f"{MODEL_TITLES[model_name]} ✓" if active else MODEL_TITLES[model_name]
            self._style_button(button, title, role, enabled)

    @objc.python_method
    def _refresh_translation_toggle(self) -> None:
        if self.englishToggleButton is None:
            return

        enabled = self._translation_toggle_enabled()
        role = "toggle_on" if self.transcriber.translate_to_english_enabled() else "toggle_off"
        self._style_button(self.englishToggleButton, "EN", role, enabled)

    @objc.python_method
    def _translation_toggle_enabled(self) -> bool:
        return (
            not self.recording
            and not self.paused
            and not self.transcribing
            and self.transcriber.current_model_supports_translation()
        )

    @objc.python_method
    def _style_button(self, button, title: str, role: str, enabled: bool) -> None:
        if button is None:
            return

        button.setTitle_(title)
        button.setEnabled_(enabled)
        if button == self.englishToggleButton and button.layer() is not None:
            button.layer().setBackgroundColor_(self._button_fill_color(role, enabled).CGColor())
            button.layer().setBorderColor_(self._button_border_color(role, enabled).CGColor())
        button.setBezelColor_(self._button_fill_color(role, enabled))
        button.setContentTintColor_(self._button_text_color(role, enabled))

    @objc.python_method
    def _card_background(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.07, 0.09, 0.13, 0.97)

    @objc.python_method
    def _transcript_background(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.045, 0.06, 0.09, 1.0)

    @objc.python_method
    def _border_color(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.19, 0.22, 0.28, 1.0)

    @objc.python_method
    def _wave_color(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.34, 0.78, 0.98, 1.0)

    @objc.python_method
    def _button_fill_color(self, role: str, enabled: bool):
        if not enabled:
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.18, 0.23, 0.95)
        if role == "record":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.43, 0.85, 1.0)
        if role == "toggle_on":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.18, 0.47, 0.88, 1.0)
        if role == "toggle_off":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.17, 0.22, 0.30, 1.0)
        if role == "stop":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.72, 0.24, 0.26, 1.0)
        if role == "selector":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.17, 0.22, 0.30, 1.0)
        if role == "ghost":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.16, 0.22, 0.95)
        if role == "active_menu":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.43, 0.85, 0.95)
        if role == "menu":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.18, 0.24, 0.95)
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.17, 0.21, 0.28, 1.0)

    @objc.python_method
    def _button_text_color(self, role: str, enabled: bool):
        if not enabled:
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.48, 0.54, 0.63, 1.0)
        if role == "toggle_on":
            return self._text_primary()
        if role == "toggle_off":
            return self._text_secondary()
        if role == "menu":
            return self._text_secondary()
        return self._text_primary()

    @objc.python_method
    def _button_border_color(self, role: str, enabled: bool):
        if not enabled:
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.25, 0.31, 1.0)
        if role == "toggle_on":
            return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.33, 0.60, 0.96, 1.0)
        if role == "toggle_off":
            return self._border_color()
        return self._border_color()

    @objc.python_method
    def _text_primary(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.93, 0.95, 0.98, 1.0)

    @objc.python_method
    def _text_secondary(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.67, 0.71, 0.78, 1.0)

    @objc.python_method
    def _green(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.77, 0.33, 1.0)

    @objc.python_method
    def _orange(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.62, 0.14, 1.0)

    @objc.python_method
    def _red(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.94, 0.27, 0.27, 1.0)

    @objc.python_method
    def _pink(self):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.33, 0.55, 1.0)


_DELEGATE = None


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--download-model", choices=MODEL_OPTIONS)
    args = parser.parse_args()

    if args.download_model:
        transcriber = WhisperTranscriber()
        path = transcriber.download_model(args.download_model, progress_callback=print)
        print(f"Ready: {path}")
        return

    global _DELEGATE
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _DELEGATE = AppDelegate.alloc().init()
    app.setDelegate_(_DELEGATE)
    app.run()


if __name__ == "__main__":
    main()
