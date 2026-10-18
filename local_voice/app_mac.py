from __future__ import annotations

import argparse
import math
import queue
import threading
from dataclasses import dataclass

import objc
import Quartz  # Registers CGColorRef and CALayer metadata with the Objective-C bridge.
from AppKit import (
    NSAlert,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSRoundedBezelStyle,
    NSColor,
    NSControl,
    NSFloatingWindowLevel,
    NSFont,
    NSMenu,
    NSMenuItem,
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
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable,
)
from Foundation import NSObject, NSTimer

from .paths import app_data_dir
from .hotkeys import SHORTCUT, global_hotkey
from .prompts import delete_prompt, load_prompts, pin_transcript, save_prompts
from .recent import RecentTranscripts
from .recorder import Recorder
from .sessions import ChunkedRecordingSession
from .workspace import cleanup_stale_workspaces
from .transcriber import MODEL_OPTIONS, MODEL_TITLES, WhisperTranscriber

BASE_WINDOW_WIDTH = 248
BASE_WINDOW_HEIGHT = 158
UI_SCALE = 0.85
WINDOW_WIDTH = round(BASE_WINDOW_WIDTH * UI_SCALE)
WINDOW_HEIGHT = round(BASE_WINDOW_HEIGHT * UI_SCALE)
COMPACT_WIDTH = round(160 * UI_SCALE)
COMPACT_HEIGHT = round(36 * UI_SCALE)
BASE_POPUP_WIDTH = 420
BASE_POPUP_HEIGHT = 380
POPUP_WIDTH = round(BASE_POPUP_WIDTH * UI_SCALE)
POPUP_HEIGHT = round(BASE_POPUP_HEIGHT * UI_SCALE)
IDLE_TEXT = "Record, speak, stop."


@dataclass
class UiEvent:
    kind: str
    payload: str = ""
    generation: int = 0
    partial_text: str = ""


class TranscriptRow(NSControl):
    """Open the transcript from any row area except its individual buttons."""

    def hitTest_(self, point):
        hit = objc.super(TranscriptRow, self).hitTest_(point)
        if hit is None:
            return None
        view = hit
        while view is not None and view != self:
            if isinstance(view, NSButton):
                return hit
            view = view.superview()
        return self

    def mouseDown_(self, event) -> None:
        self.sendAction_to_(self.action(), self.target())


class AppDelegate(NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None

        cleanup_stale_workspaces()
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
        self.sessionGeneration = 0
        self.cancelling = False
        self.progressLabel = None
        self.panel = None
        self.compact = True
        self.expandedView = None
        self.compactView = None
        self.compactStatusLabel = None
        self.compactStatusDot = None
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
        self.recentTranscripts = RecentTranscripts()
        self.recordingHotkey = None
        self.promptsPanel = None
        self.promptsButton = None
        self.recentScrollView = None
        self.recentStackContainer = None
        self.promptsScrollView = None
        self.promptsStackContainer = None
        self.libraryLayout = []
        self.savedLibrarySize = None
        return self

    def applicationDidFinishLaunching_(self, notification) -> None:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._setup_menu(app)
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
        self._start_recording_hotkey()
        self._start_model_warmup()

    def applicationShouldTerminateAfterLastWindowClosed_(self, app) -> bool:
        return False

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
            print("[Local Voice] Recording paused")
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
            print("[Local Voice] Recording resumed")
            return

        try:
            self.recorder.start()
        except Exception as exc:
            self._set_status("Mic error", self._pink())
            self._set_transcript(str(exc))
            return

        self.sessionGeneration += 1
        generation = self.sessionGeneration
        try:
            self.recordingSession = ChunkedRecordingSession(
                self.recorder, self.transcriber,
                progress_callback=lambda message: self.ui_queue.put(UiEvent("progress", message, generation)),
                error_callback=lambda message: self.ui_queue.put(UiEvent("recording_failed", message, generation)),
            )
            self.recordingSession.ui_generation = generation
        except Exception as exc:
            self.recorder.stop()
            self._set_status("Save error", self._pink())
            self._set_transcript(str(exc))
            return
        self.progressLabel.setStringValue_("")
        self.progressLabel.setToolTip_(None)
        self.recording = True
        self.paused = False
        self._hide_model_menu()
        self._set_status("Recording", self._red())
        self._set_loading(False)
        self._set_transcript("")
        self._refresh_buttons()
        print("[Local Voice] Recording started")

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
        print("[Local Voice] Stopping — transcribing...")
        thread = threading.Thread(target=self._finalize_recording_session, args=(session,), daemon=True)
        thread.start()

    def copyClicked_(self, sender) -> None:
        text = self._current_text()
        if not text:
            return
        self._copy_to_clipboard(text)

    @objc.python_method
    def _copy_to_clipboard(self, text) -> bool:
        try:
            pasteboard = NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            if not pasteboard.setString_forType_(text, NSPasteboardTypeString):
                raise RuntimeError("macOS could not write the transcript to the clipboard")
        except Exception as exc:
            self._set_status("Copy failed", self._orange())
            self.copyButton.setToolTip_(f"Transcript is still available. Click to retry copying. {exc}")
            print(f"[Local Voice] Clipboard copy failed: {exc}")
            return False
        self.copyButton.setToolTip_("Copy transcript · finished recordings are also copied automatically")
        self._set_status("Copied", self._green())
        return True

    @objc.python_method
    def _start_recording_hotkey(self) -> None:
        def pressed():
            # Do not leave a latent toggle queued while a prompt-editing dialog is open.
            if NSApplication.sharedApplication().modalWindow() is None:
                self.ui_queue.put(UiEvent("shortcut"))

        try:
            self.recordingHotkey = global_hotkey(pressed)
            self.recordingHotkey.start()
            self.recordButton.setToolTip_(f"Record / Pause · {SHORTCUT} starts or finishes recording")
            self.stopButton.setToolTip_(f"Finish transcription · {SHORTCUT}")
            print(f"[Local Voice] Global shortcut ready: {SHORTCUT}")
        except Exception as exc:
            self.recordingHotkey = None
            self.progressLabel.setStringValue_("Shortcut unavailable · use Rec / Stop")
            self.progressLabel.setToolTip_(str(exc))
            print(f"[Local Voice] Global shortcut unavailable: {exc}")

    @objc.python_method
    def _recording_shortcut(self) -> None:
        if not self.model_ready or self.loading or self.transcribing or self.switching_model or self.cancelling:
            return
        if self.recording or self.paused:
            self.stopClicked_(None)
        else:
            self.recordClicked_(None)

    def cancelClicked_(self, sender) -> None:
        if self.recordingSession is None or self.cancelling:
            return

        session = self.recordingSession
        self.sessionGeneration += 1
        generation = self.sessionGeneration
        self.recording = False
        self.paused = False
        self.transcribing = True
        self.cancelling = True
        self._set_status("Cancelling", self._orange())
        self._refresh_buttons()

        def cancel():
            try:
                session.cancel()
                self.ui_queue.put(UiEvent("cancelled", generation=generation))
            except Exception as exc:
                self.ui_queue.put(UiEvent("error", str(exc), generation))

        threading.Thread(target=cancel, daemon=True).start()

    def quitClicked_(self, sender) -> None:
        NSApplication.sharedApplication().terminate_(None)

    def toggleCompactClicked_(self, sender) -> None:
        self.compact = not self.compact
        self._apply_widget_mode()

    def applicationWillTerminate_(self, notification) -> None:
        self._save_library_size()
        if self.recordingHotkey is not None:
            self.recordingHotkey.close()
            self.recordingHotkey = None
        if self.recordingSession is not None:
            self.recordingSession.cancel()
        self.transcriber.unload_model()

    def promptsClicked_(self, sender) -> None:
        if self.promptsPanel is None:
            self._build_prompts_panel()
        if self.promptsPanel.isVisible():
            self._save_library_size()
            self.promptsPanel.orderOut_(None)
        else:
            self._position_prompts_panel()
            self._refresh_prompts_panel()
            self.promptsPanel.orderFrontRegardless()

    def promptsPanelCloseClicked_(self, sender) -> None:
        if self.promptsPanel is not None:
            self._save_library_size()
            self.promptsPanel.orderOut_(None)

    def windowDidResize_(self, notification) -> None:
        if notification.object() == self.promptsPanel:
            self._layout_prompts_panel()

    def windowDidEndLiveResize_(self, notification) -> None:
        if notification.object() == self.promptsPanel:
            self._save_library_size()

    def copyRecentClicked_(self, sender) -> None:
        entry = self.recentTranscripts.get(sender.tag())
        if entry is not None:
            self._copy_library_text(entry.text, sender)

    def pinRecentClicked_(self, sender) -> None:
        entry = self.recentTranscripts.get(sender.tag())
        if entry is None:
            return
        try:
            pin_transcript(entry.text)
        except OSError as exc:
            self._show_library_error("Could not pin transcript", exc)
            return
        self._refresh_prompts_panel()

    def viewRecentClicked_(self, sender) -> None:
        entry = self.recentTranscripts.get(sender.tag())
        if entry is None:
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Transcript · {entry.created_at.strftime('%H:%M')}")
        alert.addButtonWithTitle_("Copy")
        alert.addButtonWithTitle_("Close")
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 190))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)
        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 190))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setRichText_(False)
        text_view.setFont_(NSFont.systemFontOfSize_(NSFont.systemFontSize()))
        text_view.setString_(entry.text)
        text_view.setHorizontallyResizable_(False)
        text_view.setVerticallyResizable_(True)
        text_view.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(text_view)
        alert.setAccessoryView_(scroll)
        if alert.runModal() == 1000:
            self._copy_library_text(entry.text)

    @objc.python_method
    def _copy_library_text(self, text, sender=None) -> None:
        try:
            pasteboard = NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            if not pasteboard.setString_forType_(text, NSPasteboardTypeString):
                raise RuntimeError("macOS could not write to the clipboard. Try copying again.")
        except Exception as exc:
            self._show_library_error("Could not copy transcript", exc)
            return
        if sender is not None:
            sender.setTitle_("Copied")

    @objc.python_method
    def _show_library_error(self, title, error) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(str(error))
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    def copyPromptClicked_(self, sender) -> None:
        index = sender.tag()
        prompts = load_prompts()
        if 0 <= index < len(prompts):
            self._copy_library_text(prompts[index]["text"], sender)

    def deletePromptClicked_(self, sender) -> None:
        index = sender.tag()
        try:
            delete_prompt(index)
        except OSError as exc:
            self._show_library_error("Could not unpin transcript", exc)
            return
        self._refresh_prompts_panel()

    def viewPromptClicked_(self, sender) -> None:
        index = sender.tag()
        prompts = load_prompts()
        if not 0 <= index < len(prompts):
            return
        entry = prompts[index]

        alert = NSAlert.alloc().init()
        alert.setMessageText_(entry["name"])
        alert.setInformativeText_("Rename this pinned item or edit its text, then click Update.")
        alert.addButtonWithTitle_("Update")
        alert.addButtonWithTitle_("Cancel")

        accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 106))

        name_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 82, 340, 22))
        name_field.setStringValue_(entry["name"])
        accessory.addSubview_(name_field)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 78))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(2)

        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 78))
        text_view.setEditable_(True)
        text_view.setSelectable_(True)
        text_view.setRichText_(False)
        text_view.setFont_(NSFont.systemFontOfSize_(NSFont.systemFontSize()))
        text_view.textContainer().setWidthTracksTextView_(True)
        text_view.setString_(entry["text"])
        scroll.setDocumentView_(text_view)
        accessory.addSubview_(scroll)

        alert.setAccessoryView_(accessory)
        alert.window().setInitialFirstResponder_(name_field)

        if alert.runModal() == 1000:
            new_name = name_field.stringValue().strip()
            new_text = text_view.string().strip()
            if new_name and new_text:
                prompts[index] = {"name": new_name, "text": new_text}
                try:
                    save_prompts(prompts)
                except OSError as exc:
                    self._show_library_error("Could not update pinned transcript", exc)
                    return
                self._refresh_prompts_panel()

    def toggleEnglishClicked_(self, sender) -> None:
        if self.englishToggleButton is None or not self._translation_toggle_enabled():
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

            if event.generation and event.generation != self.sessionGeneration:
                continue

            if event.kind == "shortcut":
                self._recording_shortcut()
            elif event.kind == "transcript":
                self.recordingSession = None
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload or "No speech recognized.")
                self._set_status("Ready", self._green())
                self._refresh_buttons()
                if event.payload:
                    self.recentTranscripts.add(event.payload)
                    words = len(event.payload.split())
                    print(f"[Local Voice] Transcript ready ({words} words)")
                    if self.promptsPanel is not None and self.promptsPanel.isVisible():
                        self._refresh_prompts_panel()
                    self._copy_to_clipboard(event.payload)
            elif event.kind == "ready":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", self._green())
                self._refresh_buttons()
                print(f"[Local Voice] Model ready ({self.transcriber.current_model_name()})")
            elif event.kind == "model_switched":
                self.switching_model = False
                self.model_ready = True
                self._set_loading(False)
                self._ensure_idle_text()
                self._set_status("Ready", self._green())
                self._refresh_buttons()
                print(f"[Local Voice] Switched to {self.transcriber.current_model_name()}")
            elif event.kind == "error":
                self.recordingSession = None
                self.cancelling = False
                self.switching_model = False
                self.recording = False
                self.paused = False
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.partial_text or event.payload)
                if event.partial_text:
                    self.progressLabel.setStringValue_("Interrupted · showing completed text")
                    self.progressLabel.setToolTip_(event.payload)
                self._set_status("Failed", self._pink())
                self._refresh_buttons()
                print(f"[Local Voice] Error: {event.payload}")
            elif event.kind == "no_audio":
                self.recordingSession = None
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(event.payload)
                self._set_status("No audio", self._orange())
                self._refresh_buttons()
                print("[Local Voice] No audio captured")
            elif event.kind == "progress":
                self.progressLabel.setStringValue_(event.payload)
                self.progressLabel.setToolTip_(event.payload)
                print(f"[Local Voice] {event.payload}")
            elif event.kind == "cancelled":
                self.recordingSession = None
                self.cancelling = False
                self.transcribing = False
                self._set_loading(False)
                self._set_transcript(IDLE_TEXT)
                self._set_status("Ready", self._green())
                self.progressLabel.setStringValue_("")
                self.progressLabel.setToolTip_(None)
                self._refresh_buttons()
            elif event.kind == "recording_failed":
                self.progressLabel.setStringValue_(event.payload)
                self.stopClicked_(None)
        self._refresh_content_mode()

    def refreshVisualizer_(self, timer) -> None:
        self._advance_waveform()

    @objc.python_method
    def _finalize_recording_session(self, session: ChunkedRecordingSession) -> None:
        try:
            def _on_progress(message: str) -> None:
                self.ui_queue.put(UiEvent("progress", message, session.ui_generation))

            text = session.stop_recording_and_wait(progress_callback=_on_progress)
            if text is None:
                self.ui_queue.put(UiEvent("no_audio", "No audio was captured. Try again.", session.ui_generation))
                return
            self.ui_queue.put(UiEvent("transcript", text, session.ui_generation))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc), session.ui_generation, getattr(exc, "partial_text", "")))

    @objc.python_method
    def _warm_model_in_background(self) -> None:
        try:
            def on_progress(msg: str) -> None:
                print(f"[Local Voice] {msg}")

            self.transcriber._load_model(progress_callback=on_progress)
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
                f"{model_name} is not cached yet.\n\nRun:\n./start.cmd --download-model {model_name}"
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
            def on_progress(msg: str) -> None:
                print(f"[Local Voice] {msg}")

            self.transcriber.switch_model(model_name, progress_callback=on_progress)
            self.transcriber.save_model_preference(model_name)
            self.ui_queue.put(UiEvent("model_switched"))
        except Exception as exc:
            self.ui_queue.put(UiEvent("error", str(exc)))

    @objc.python_method
    def _build_prompts_panel(self) -> None:
        width, height = self._load_library_size()
        self.promptsPanel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        self.promptsPanel.setLevel_(NSFloatingWindowLevel)
        self.promptsPanel.setOpaque_(False)
        self.promptsPanel.setHasShadow_(True)
        self.promptsPanel.setBackgroundColor_(NSColor.clearColor())
        self.promptsPanel.setMovableByWindowBackground_(True)
        self.promptsPanel.setHidesOnDeactivate_(False)
        self.promptsPanel.setMinSize_((POPUP_WIDTH, POPUP_HEIGHT))
        self.promptsPanel.setShowsResizeIndicator_(True)
        self.promptsPanel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, POPUP_WIDTH, POPUP_HEIGHT))
        content.setAutoresizesSubviews_(False)
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(self._scaled(15.0))
        content.layer().setBackgroundColor_(self._card_background().CGColor())
        content.layer().setBorderWidth_(1.0)
        content.layer().setBorderColor_(self._border_color().CGColor())
        self.promptsPanel.setContentView_(content)

        # Row 1 — close button, far top-right (separate row from copy button)
        close_btn = self._make_button(
            self._rect(BASE_POPUP_WIDTH - 24, BASE_POPUP_HEIGHT - 24, 18, 18),
            "×",
            "promptsPanelCloseClicked:",
            role="ghost",
            control_size=NSMiniControlSize,
            font_size=11.5,
        )
        content.addSubview_(close_btn)

        title = self._make_label(
            self._rect(14, BASE_POPUP_HEIGHT - 34, 260, 20), "Recent & Pinned", 12, True
        )
        content.addSubview_(title)
        recent_label = self._make_label(
            self._rect(14, BASE_POPUP_HEIGHT - 59, 90, 16), "Recent", 10, True
        )
        content.addSubview_(recent_label)
        recent_hint = self._make_label(
            self._rect(108, BASE_POPUP_HEIGHT - 58, BASE_POPUP_WIDTH - 122, 14),
            "Last 10 · cleared on quit", 8.5, False,
        )
        recent_hint.setTextColor_(self._text_secondary())
        recent_hint.setAlignment_(2)
        content.addSubview_(recent_hint)

        recent_frame = self._rect(10, BASE_POPUP_HEIGHT - 204, BASE_POPUP_WIDTH - 20, 138)
        self.recentScrollView = NSScrollView.alloc().initWithFrame_(recent_frame)
        self.recentScrollView.setHasVerticalScroller_(True)
        self.recentScrollView.setBorderType_(0)
        self.recentScrollView.setDrawsBackground_(False)
        self.recentStackContainer = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, recent_frame.size.width, 0)
        )
        self.recentScrollView.setDocumentView_(self.recentStackContainer)
        content.addSubview_(self.recentScrollView)

        # Divider
        divider = NSView.alloc().initWithFrame_(
            self._rect(14, BASE_POPUP_HEIGHT - 216, BASE_POPUP_WIDTH - 28, 1)
        )
        divider.setWantsLayer_(True)
        divider.layer().setBackgroundColor_(self._border_color().CGColor())
        content.addSubview_(divider)

        saved_label = self._make_label(
            self._rect(14, BASE_POPUP_HEIGHT - 237, 90, 16), "Pinned", 10, True
        )
        content.addSubview_(saved_label)
        pinned_hint = self._make_label(
            self._rect(108, BASE_POPUP_HEIGHT - 236, BASE_POPUP_WIDTH - 122, 14),
            "Saved on this computer", 8.5, False,
        )
        pinned_hint.setTextColor_(self._text_secondary())
        pinned_hint.setAlignment_(2)
        content.addSubview_(pinned_hint)

        # Pinned text has its own scroll area so it stays accessible beside Recent.
        scroll_frame = self._rect(10, 14, BASE_POPUP_WIDTH - 20, BASE_POPUP_HEIGHT - 260)
        self.promptsScrollView = NSScrollView.alloc().initWithFrame_(scroll_frame)
        self.promptsScrollView.setHasVerticalScroller_(True)
        self.promptsScrollView.setBorderType_(0)
        self.promptsScrollView.setDrawsBackground_(False)

        self.promptsStackContainer = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, scroll_frame.size.width, 0)
        )
        self.promptsScrollView.setDocumentView_(self.promptsStackContainer)
        content.addSubview_(self.promptsScrollView)

        # Growth factors keep the header at the top and share extra height between lists.
        self.libraryLayout = [
            (view, view.frame(), dx, dy, dw, dh)
            for view, dx, dy, dw, dh in (
                (close_btn, 1, 1, 0, 0),
                (title, 0, 1, 1, 0),
                (recent_label, 0, 1, 0, 0),
                (recent_hint, 0, 1, 1, 0),
                (self.recentScrollView, 0, 0.5, 1, 0.5),
                (divider, 0, 0.5, 1, 0),
                (saved_label, 0, 0.5, 0, 0),
                (pinned_hint, 0, 0.5, 1, 0),
                (self.promptsScrollView, 0, 0, 1, 0.5),
            )
        ]
        self.promptsPanel.setDelegate_(self)
        self._layout_prompts_panel()

    @objc.python_method
    def _load_library_size(self):
        size = self.transcriber._load_settings().get("library_size")
        if (isinstance(size, (list, tuple)) and len(size) == 2
                and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and 0 < value <= 10000 for value in size)):
            self.savedLibrarySize = tuple(size)
            return max(POPUP_WIDTH, size[0]), max(POPUP_HEIGHT, size[1])
        return POPUP_WIDTH, POPUP_HEIGHT

    @objc.python_method
    def _save_library_size(self) -> None:
        if self.promptsPanel is None:
            return
        size = self.promptsPanel.frame().size
        current = (round(size.width), round(size.height))
        if current == self.savedLibrarySize:
            return
        try:
            self.transcriber._write_settings({"library_size": list(current)})
        except OSError as exc:
            print(f"[Local Voice] Could not remember popup size: {exc}")
        else:
            self.savedLibrarySize = current

    @objc.python_method
    def _layout_prompts_panel(self) -> None:
        if not self.libraryLayout:
            return
        lists = ((self.recentScrollView, self.recentStackContainer),
                 (self.promptsScrollView, self.promptsStackContainer))
        offsets = []
        for scroll, container in lists:
            clip = scroll.contentView().bounds()
            offsets.append(max(0, container.frame().size.height - clip.size.height - clip.origin.y))
        size = self.promptsPanel.contentView().bounds().size
        extra_w, extra_h = size.width - POPUP_WIDTH, size.height - POPUP_HEIGHT
        for view, frame, dx, dy, dw, dh in self.libraryLayout:
            view.setFrame_(NSMakeRect(
                frame.origin.x + dx * extra_w, frame.origin.y + dy * extra_h,
                frame.size.width + dw * extra_w, frame.size.height + dh * extra_h,
            ))
        for (scroll, container), offset in zip(lists, offsets):
            scroll.tile()
            clip = scroll.contentView().bounds()
            height = max(len(container.subviews()) * self._scaled(46), clip.size.height)
            container.setFrame_(NSMakeRect(0, 0, clip.size.width, height))
            scroll.contentView().scrollToPoint_((0, max(0, height - clip.size.height - offset)))
            scroll.reflectScrolledClipView_(scroll.contentView())

    @objc.python_method
    def _position_prompts_panel(self) -> None:
        if self.panel is None or self.promptsPanel is None:
            return
        frame = self.panel.frame()
        size = self.promptsPanel.frame().size
        width, height = size.width, size.height
        screen = self.panel.screen() or NSScreen.mainScreen()
        if screen is not None:
            visible = screen.visibleFrame()
            width = min(width, visible.size.width)
            height = min(height, visible.size.height)
            self.promptsPanel.setMaxSize_((visible.size.width, visible.size.height))
            self.promptsPanel.setContentSize_((width, height))
        x = frame.origin.x - width - 8
        y = frame.origin.y + frame.size.height - height
        if screen is not None:
            if x < visible.origin.x:
                x = frame.origin.x + frame.size.width + 8
            x = max(visible.origin.x, min(x, visible.origin.x + visible.size.width - width))
            y = max(visible.origin.y, min(y, visible.origin.y + visible.size.height - height))
        self.promptsPanel.setFrameOrigin_((x, y))

    @objc.python_method
    def _refresh_prompts_panel(self) -> None:
        if self.recentStackContainer is None or self.promptsStackContainer is None:
            return
        prompts = load_prompts()
        pinned_text = {entry["text"].strip() for entry in prompts}
        recent_rows = [dict(
            title=f"{entry.created_at.strftime('%H:%M')} · {len(entry.text.split())} words ▸",
            text=entry.text, tag=entry.identifier, view="viewRecentClicked:", copy="copyRecentClicked:",
            action="pinRecentClicked:", action_title="Pinned" if entry.text in pinned_text else "Pin",
            action_enabled=entry.text not in pinned_text,
        ) for entry in self.recentTranscripts.entries]
        pinned_rows = [dict(
            title=entry["name"] + " ▸", text=entry["text"], tag=index,
            view="viewPromptClicked:", copy="copyPromptClicked:", action="deletePromptClicked:",
            action_title="Unpin", action_enabled=True,
        ) for index, entry in enumerate(prompts)]
        self._render_library_list(self.recentScrollView, self.recentStackContainer, recent_rows,
                                  "Your next transcript will appear here.")
        self._render_library_list(self.promptsScrollView, self.promptsStackContainer, pinned_rows,
                                  "Pin a transcript to keep it for later.")

    @objc.python_method
    def _render_library_list(self, scroll, container, entries, empty_message) -> None:
        for view in list(container.subviews()):
            view.removeFromSuperview()
        scroll_frame = scroll.contentView().bounds()
        container_w = scroll_frame.size.width
        row_h = self._scaled(46)
        total_h = max(row_h * len(entries), scroll_frame.size.height)
        container.setFrame_(NSMakeRect(0, 0, container_w, total_h))
        if not entries:
            empty_label = self._make_label(
                NSMakeRect(0, (total_h - self._scaled(14)) / 2, container_w, self._scaled(14)),
                empty_message, 9, False,
            )
            empty_label.setTextColor_(self._text_secondary())
            empty_label.setAlignment_(1)
            empty_label.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin | NSViewMaxYMargin)
            container.addSubview_(empty_label)

        for i, entry in enumerate(entries):
            row_y = total_h - ((i + 1) * row_h)
            row = TranscriptRow.alloc().initWithFrame_(NSMakeRect(0, row_y, container_w, row_h))
            row.setTarget_(self)
            row.setAction_(entry["view"])
            row.setTag_(entry["tag"])
            row.setToolTip_("Open transcript")
            row.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
            name_w = container_w - self._scaled(122)
            view_button = self._make_button(
                NSMakeRect(self._scaled(8), self._scaled(24), name_w, self._scaled(18)),
                entry["title"], entry["view"], role="ghost",
                control_size=NSMiniControlSize, font_size=9,
            )
            view_button.setBordered_(False)
            view_button.setAlignment_(0)
            view_button.cell().setLineBreakMode_(4)
            view_button.setTag_(entry["tag"])
            view_button.setAutoresizingMask_(NSViewWidthSizable)
            view_button.setToolTip_("View full transcript" if entry["view"] == "viewRecentClicked:" else "View or rename pinned text")
            row.addSubview_(view_button)
            preview = self._make_label(
                NSMakeRect(self._scaled(8), self._scaled(5), name_w, self._scaled(16)),
                " ".join(entry["text"].split())[:180], 9, False,
            )
            preview.setTextColor_(self._text_secondary())
            preview.setLineBreakMode_(4)
            preview.setAutoresizingMask_(NSViewWidthSizable)
            row.addSubview_(preview)
            copy_btn = self._make_button(
                NSMakeRect(container_w - self._scaled(110), self._scaled(14), self._scaled(50), self._scaled(20)),
                "Copy", entry["copy"], role="ghost", control_size=NSMiniControlSize, font_size=8.5,
            )
            copy_btn.setTag_(entry["tag"])
            copy_btn.setAutoresizingMask_(NSViewMinXMargin)
            copy_btn.setToolTip_("Copy transcript")
            row.addSubview_(copy_btn)
            action_btn = self._make_button(
                NSMakeRect(container_w - self._scaled(56), self._scaled(14), self._scaled(48), self._scaled(20)),
                entry["action_title"], entry["action"], role="ghost",
                control_size=NSMiniControlSize, font_size=8.5,
            )
            action_btn.setTag_(entry["tag"])
            action_btn.setAutoresizingMask_(NSViewMinXMargin)
            action_btn.setEnabled_(entry["action_enabled"])
            action_btn.setToolTip_("Remove from pinned items" if entry["action_title"] == "Unpin" else "Keep this transcript after quitting")
            row.addSubview_(action_btn)
            divider = NSView.alloc().initWithFrame_(NSMakeRect(self._scaled(8), 0, container_w - self._scaled(16), 1))
            divider.setWantsLayer_(True)
            divider.setAutoresizingMask_(NSViewWidthSizable)
            divider.layer().setBackgroundColor_(self._border_color().CGColor())
            row.addSubview_(divider)
            container.addSubview_(row)
        scroll.contentView().scrollToPoint_((0, max(0, total_h - scroll_frame.size.height)))
        scroll.reflectScrolledClipView_(scroll.contentView())

    @objc.python_method
    def _setup_menu(self, app) -> None:
        menubar = NSMenu.alloc().init()

        edit_item = NSMenuItem.alloc().init()
        menubar.addItem_(edit_item)

        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Undo", "undo:", "z")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Redo", "redo:", "Z")
        edit_menu.addItem_(NSMenuItem.separatorItem())
        edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
        edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
        edit_item.setSubmenu_(edit_menu)

        app.setMainMenu_(menubar)

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
        self.panel.disableCursorRects()

        self._position_window()
        self._build_header(content)
        self._build_transcript(content)
        self._build_model_picker(content)
        self._build_buttons(content)

        # Keep the full interface alive so toggling never rebuilds controls or session state.
        self.expandedView = content
        container = NSView.alloc().initWithFrame_(content.frame())
        container.setAutoresizesSubviews_(False)
        container.setWantsLayer_(True)
        container.layer().setCornerRadius_(self._scaled(15.0))
        container.layer().setBackgroundColor_(self._card_background().CGColor())
        container.layer().setBorderWidth_(1.0)
        container.layer().setBorderColor_(self._border_color().CGColor())
        self.panel.setContentView_(container)
        container.addSubview_(content)
        self._build_compact_view(container)
        self._apply_widget_mode()

    @objc.python_method
    def _build_compact_view(self, container) -> None:
        self.compactView = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, COMPACT_WIDTH, COMPACT_HEIGHT))
        container.addSubview_(self.compactView)
        self.compactView.setToolTip_(f"Local Voice · {SHORTCUT} starts or finishes recording")
        self.compactStatusDot = NSView.alloc().initWithFrame_(self._rect(12, 15, 6, 6))
        self.compactStatusDot.setWantsLayer_(True)
        self.compactStatusDot.layer().setCornerRadius_(self._scaled(3))
        self.compactView.addSubview_(self.compactStatusDot)
        self.compactStatusLabel = self._make_label(self._rect(25, 10, 103, 16), "Loading", 10.5, False)
        self.compactView.addSubview_(self.compactStatusLabel)
        expand = self._make_button(self._rect(130, 7, 22, 22), "▾", "toggleCompactClicked:",
                                   role="ghost", control_size=NSMiniControlSize, font_size=12)
        expand.setToolTip_("Expand Local Voice")
        expand.setAccessibilityLabel_("Expand Local Voice")
        self.compactView.addSubview_(expand)

    @objc.python_method
    def _apply_widget_mode(self) -> None:
        if self.compact:
            self._hide_model_menu()
            if self.promptsPanel is not None:
                self.promptsPanel.orderOut_(None)
        self.expandedView.setHidden_(self.compact)
        self.compactView.setHidden_(not self.compact)
        width, height = (COMPACT_WIDTH, COMPACT_HEIGHT) if self.compact else (WINDOW_WIDTH, WINDOW_HEIGHT)
        frame = self.panel.frame()
        # Keep the top-right corner fixed, including after the user drags the widget.
        x = frame.origin.x + frame.size.width - width
        y = frame.origin.y + frame.size.height - height
        screen = self.panel.screen()
        if screen is not None:
            visible = screen.visibleFrame()
            x = max(visible.origin.x, min(x, visible.origin.x + visible.size.width - width))
            y = max(visible.origin.y, min(y, visible.origin.y + visible.size.height - height))
        self.panel.setFrame_display_(NSMakeRect(x, y, width, height), True)

    @objc.python_method
    def _build_header(self, content) -> None:
        self.statusDot = NSView.alloc().initWithFrame_(self._rect(10, 39, 6, 6))
        self.statusDot.setWantsLayer_(True)
        self.statusDot.layer().setCornerRadius_(self._scaled(3.0))
        content.addSubview_(self.statusDot)

        self.statusLabel = self._make_label(self._rect(22, 35, 66, 14), "", 9, False)
        self.statusLabel.setTextColor_(self._text_secondary())
        content.addSubview_(self.statusLabel)

        self.quitButton = self._make_button(
            self._rect(BASE_WINDOW_WIDTH - 28, BASE_WINDOW_HEIGHT - 30, 18, 18),
            "×",
            "quitClicked:",
            role="ghost",
            control_size=NSMiniControlSize,
            font_size=11.5,
        )
        content.addSubview_(self.quitButton)

        self.promptsButton = self._make_button(
            self._rect(BASE_WINDOW_WIDTH - 86, BASE_WINDOW_HEIGHT - 30, 22, 18),
            "★",
            "promptsClicked:",
            role="ghost",
            control_size=NSMiniControlSize,
            font_size=10,
        )
        self.promptsButton.setToolTip_("Recent & Pinned")
        self.promptsButton.setAccessibilityLabel_("Recent & Pinned")
        content.addSubview_(self.promptsButton)

        self.englishToggleButton = self._make_button(
            self._rect(99, BASE_WINDOW_HEIGHT - 30, 42, 18),
            "EN",
            "toggleEnglishClicked:",
            role="ghost",
            control_size=NSMiniControlSize,
            font_size=9,
        )
        content.addSubview_(self.englishToggleButton)

        self.progressLabel = self._make_label(self._rect(94, 35, BASE_WINDOW_WIDTH - 104, 14), "", 8, False)
        content.addSubview_(self.progressLabel)
        collapse = self._make_button(self._rect(BASE_WINDOW_WIDTH - 57, BASE_WINDOW_HEIGHT - 30, 22, 18), "▴",
                                     "toggleCompactClicked:", role="ghost",
                                     control_size=NSMiniControlSize, font_size=11)
        collapse.setToolTip_("Collapse Local Voice")
        collapse.setAccessibilityLabel_("Collapse Local Voice")
        content.addSubview_(collapse)

    @objc.python_method
    def _build_model_picker(self, content) -> None:
        model_width = 82
        model_x = 10

        self.modelSelectorButton = self._make_button(
            self._rect(model_x, BASE_WINDOW_HEIGHT - 30, model_width, 18),
            "",
            "toggleModelMenuClicked:",
            role="selector",
            control_size=NSMiniControlSize,
            font_size=9.5,
        )
        content.addSubview_(self.modelSelectorButton)

        self.modelMenuView = NSView.alloc().initWithFrame_(self._rect(model_x, BASE_WINDOW_HEIGHT - 111, 82, 76))
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
        frame = self._rect(10, 52, BASE_WINDOW_WIDTH - 20, BASE_WINDOW_HEIGHT - 90)
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
        button_width = (BASE_WINDOW_WIDTH - 32) / 3
        self.recordButton = self._make_button(
            self._rect(10, 9, button_width, 22),
            "Rec",
            "recordClicked:",
            role="record",
            font_size=9.5,
        )
        content.addSubview_(self.recordButton)

        self.stopButton = self._make_button(
            self._rect(16 + button_width, 9, button_width, 22),
            "Stop",
            "stopClicked:",
            role="stop",
            font_size=9.5,
        )
        content.addSubview_(self.stopButton)

        self.copyButton = self._make_button(
            self._rect(22 + 2 * button_width, 9, button_width, 22),
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
        if self.recording or self.paused or (self.transcribing and self.recordingSession is not None):
            self.copyButton.setAction_("cancelClicked:")
            self._style_button(self.copyButton, "Cancel", "secondary", not self.cancelling)
        else:
            self.copyButton.setAction_("copyClicked:")
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
        if self.compactStatusLabel is not None:
            self.compactStatusLabel.setStringValue_(text)
            self.compactStatusLabel.setTextColor_(color)
            self.compactStatusDot.layer().setBackgroundColor_(color.CGColor())

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
        english_output = self.transcriber.translate_to_english_enabled()
        self._style_button(self.englishToggleButton, "EN ✓" if english_output else "EN", "ghost", enabled)
        output = "English" if english_output else "Same as spoken"
        self.englishToggleButton.setAccessibilityLabel_("Output language")
        self.englishToggleButton.setAccessibilityValue_(output)
        if not self.transcriber.current_model_supports_translation():
            tooltip = "English output requires Medium or Large. Select Large for best quality."
        elif enabled:
            tooltip = f"Output: {output}. Click to {'keep the spoken language' if english_output else 'translate speech into English'}."
        else:
            tooltip = f"Output: {output}. Available when the model is ready and recording has finished."
        self.englishToggleButton.setToolTip_(tooltip)

    @objc.python_method
    def _translation_toggle_enabled(self) -> bool:
        return (
            self.model_ready
            and not self.loading
            and not self.switching_model
            and not self.cancelling
            and not self.recording
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
            return NSColor.colorWithCalibratedWhite_alpha_(0.25, 1.0)
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
            return NSColor.colorWithCalibratedWhite_alpha_(0.43, 1.0)
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
    from . import cli
    parser = argparse.ArgumentParser(add_help=True)
    cli.arguments(parser)
    args = parser.parse_args()

    if cli.handle(args):
        return

    global _DELEGATE
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    _DELEGATE = AppDelegate.alloc().init()
    app.setDelegate_(_DELEGATE)
    print(f"[Local Voice] Starting — model: {_DELEGATE.transcriber.current_model_name()}, device: {_DELEGATE.transcriber._default_device()}")
    print(f"[Local Voice] Data dir: {app_data_dir()}")
    app.run()


if __name__ == "__main__":
    main()
