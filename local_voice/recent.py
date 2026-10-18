"""Bounded, in-memory transcripts. Only explicitly pinned text is saved elsewhere."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

RECENT_LIMIT = 10


@dataclass(frozen=True)
class RecentTranscript:
    identifier: int
    text: str
    created_at: datetime


class RecentTranscripts:
    def __init__(self):
        self._entries: deque[RecentTranscript] = deque(maxlen=RECENT_LIMIT)
        self._next_identifier = 1

    @property
    def entries(self) -> tuple[RecentTranscript, ...]:
        return tuple(reversed(self._entries))

    def add(self, text: str) -> RecentTranscript | None:
        text = text.strip()
        if not text:
            return None
        entry = RecentTranscript(self._next_identifier, text, datetime.now().astimezone())
        self._next_identifier += 1
        self._entries.append(entry)
        return entry

    def get(self, identifier: int) -> RecentTranscript | None:
        return next((entry for entry in self._entries if entry.identifier == identifier), None)
