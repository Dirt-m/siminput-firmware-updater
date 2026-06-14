"""Plumbing for long-running device operations driven through the overlay."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App


class OperationCancelled(Exception):
    """Raised inside a worker when the user aborts."""


class OperationContext:
    """Handed to operation workers. Lets them post feedback to the overlay and
    observe cancellation, without knowing anything about the UI thread."""

    def __init__(self, app: App, cancel_event: threading.Event):
        self._app = app
        self._cancel = cancel_event

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise OperationCancelled()

    def status(self, message: str) -> None:
        self._app.after(0, lambda: self._app.overlay.set_status(message))

    def log(self, message: str) -> None:
        self._app.after(0, lambda: self._app.overlay.append_log(message))

    def progress(self, fraction: float) -> None:
        self._app.after(0, lambda: self._app.overlay.set_progress(fraction))
