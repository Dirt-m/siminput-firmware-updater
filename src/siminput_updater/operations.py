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

    def _post(self, fn) -> None:
        # The window can be destroyed while a worker is still running; posting
        # to a dead Tk raises RuntimeError from the worker thread.
        if getattr(self._app, "_closing", False):
            return
        try:
            self._app.after(0, fn)
        except RuntimeError:
            pass

    def status(self, message: str) -> None:
        self._post(lambda: self._app.overlay.set_status(message))

    def log(self, message: str) -> None:
        self._post(lambda: self._app.overlay.append_log(message))

    def progress(self, fraction: float) -> None:
        self._post(lambda: self._app.overlay.set_progress(fraction))
