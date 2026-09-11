"""Live input monitoring, shared by every page.

`LiveMonitor` is the single owner of `Device.start_stream`/`stop_stream`. Pages
never touch the device stream directly: they `acquire()` while they want live
input, `subscribe()` a callback, and get merged state on the UI thread.

Why a service rather than per-page streams: the stream and ordinary commands
share one serial port. Only one thing may own the read side at a time, so the
stream has to be stopped for the duration of every command. `App.run_operation`
and `App._connect_to` call `pause()`/`resume()` around their device work, which
covers every command path in the app; pages that stay subscribed across an
operation simply see the stream go quiet and come back.

Hazard this exists to respect: the firmware writes stream frames with a 0.5 s
write timeout from inside its 200 Hz main loop (`lib/serial_handler.py`,
`_send`). A host that stops draining the CDC port therefore stalls the box for
up to half a second per frame. Two rules follow, and both are load-bearing:
the serial reader thread must drain continuously while streaming, and the
stream must be stopped before any command runs.

The firmware also clears its own `_streaming` flag on a failed or partial write
and never tells the host, so a stream can die silently. `should_rearm` decides
when to re-issue `stream_start` to revive it.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

from .applog import log

if TYPE_CHECKING:
    from .app import App

STREAM_INTERVAL_MS = 50
SILENCE_S = 3.0        # no frame for this long → the stream may be dead
REARM_GAP_S = 3.0      # never re-issue stream_start more often than this
WATCHDOG_TICK_MS = 1000
RETRY_MS = 1000        # delay before restarting a stream that died

State = dict


def should_rearm(now: float, last_frame: float, last_rearm: float,
                 silence: float = SILENCE_S, gap: float = REARM_GAP_S) -> bool:
    """Should the host re-issue stream_start?

    The firmware only sends on change, so silence is ambiguous: the box may be
    idle, or its stream may have died on a failed write with no notification.
    Re-issuing is cheap (one short command, one bare ok, one snapshot frame),
    so silence past `silence` seconds is treated as suspicious — but never
    more often than every `gap` seconds, so a genuinely idle box is not spammed.
    """
    return (now - last_frame) >= silence and (now - last_rearm) >= gap


class StateMerger:
    """Folds evdev and serial stream frames into one live input state.

    MERGE RULE: while evdev is active it is the sole authority for buttons and
    axes, and "b"/"a" in serial frames are ignored outright. The serial stream
    lags evdev by up to one stream interval, so a late serial frame would
    otherwise un-press a button evdev has already reported as down. Pins only
    ever come from serial frames — evdev exposes HID output, not pin state.

    Serial "p" is changes-only and the board's pin set never changes, so every
    frame is merged with a plain update. What snapshot frames (the first frame
    after each stream_start) change is the *changed* set: it is empty for them,
    because a snapshot restates every pin rather than reporting transitions.
    Consumers that act on a press — a future "Learn this pin" button — key off
    `changed` and so never fire on a snapshot.

    Pins claimed by ENCODER rules are deinit'd by the firmware and read False
    forever; that is the device's behaviour, not a merge artefact.

    Serial "an" (firmware 2.7+) carries the raw 16-bit sample of every claimed
    analog pin, present only when one moved past the ADC noise floor or in
    the first frame. Absent means unchanged, so it merges into the kept map
    and values stay ints — a raw sample of 1 is not "pressed".
    """

    def __init__(self, evdev_active: bool = False):
        self.evdev_active = evdev_active
        self.buttons: set[int] = set()
        self.axes: list[int] = [32767] * 8
        self.pins: dict[str, bool] = {}
        self.analog: dict[str, int] = {}

    def feed(self, frame: dict) -> State:
        src = frame.get("src", "serial")
        snapshot = bool(frame.get("snapshot"))
        from_evdev = src == "evdev"

        if from_evdev or not self.evdev_active:
            if isinstance(frame.get("b"), (list, tuple, set)):
                self.buttons = {int(b) for b in frame["b"]}
            axes = frame.get("a")
            if isinstance(axes, (list, tuple)):
                vals = [int(v) for v in axes[:8]]
                vals += [32767] * (8 - len(vals))
                self.axes = vals

        changed: set[str] = set()
        pins = frame.get("p")
        if not from_evdev and isinstance(pins, dict):
            for name, raw in pins.items():
                val = bool(raw)
                if not snapshot and self.pins.get(name) != val:
                    changed.add(name)
                self.pins[name] = val

        analog = frame.get("an")
        if not from_evdev and isinstance(analog, dict):
            for name, raw in analog.items():
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                self.analog[str(name)] = int(raw)

        return self.state(changed=changed, snapshot=snapshot)

    def state(self, changed: set[str] | None = None, snapshot: bool = False) -> State:
        return {
            "b": set(self.buttons),
            "a": list(self.axes),
            "p": dict(self.pins),
            "an": dict(self.analog),
            "changed": set(changed or ()),
            "snapshot": snapshot,
        }


class LiveMonitor:
    """Owns the device stream and fans merged state out to subscribed pages."""

    def __init__(self, app: App):
        self.app = app
        self._subs: list[Callable[[State], None]] = []
        self._want = 0            # pages that want live input right now
        self._pause_depth = 0     # operations holding the stream off
        self._active = False
        self._lock = threading.Lock()
        self._merger = StateMerger()
        self._pending: State | None = None
        self._pending_changed: set[str] = set()
        self._delivery_scheduled = False
        self._last_frame = 0.0
        self._last_rearm = 0.0
        self._watchdog_id: str | None = None
        self._retry_id: str | None = None
        app.register_connection_listener(self._on_connection)

    # ------------------------------------------------------------ subscribers

    @property
    def active(self) -> bool:
        return self._active

    def subscribe(self, fn: Callable[[State], None]) -> None:
        if fn not in self._subs:
            self._subs.append(fn)
        if self._active:
            # Frames are changes-only: a page that subscribes mid-stream would
            # otherwise sit blank until something moves.
            with self._lock:
                state = self._merger.state()
            self._call(fn, state)

    def unsubscribe(self, fn: Callable[[State], None]) -> None:
        if fn in self._subs:
            self._subs.remove(fn)

    # -------------------------------------------------------------- demand

    def acquire(self) -> None:
        self._want += 1
        self._evaluate()

    def release(self) -> None:
        self._want = max(0, self._want - 1)
        self._evaluate()

    def pause(self) -> None:
        """Hold the stream off while a command owns the serial port."""
        self._pause_depth += 1
        self._evaluate()

    def resume(self) -> None:
        self._pause_depth = max(0, self._pause_depth - 1)
        # A device that rebooted mid-operation (set_config) has forgotten it
        # was streaming; _start always re-issues stream_start, so resuming
        # after a reconnect is enough to bring the stream back.
        self._evaluate()

    def shutdown(self) -> None:
        self._want = 0
        self._evaluate()

    # -------------------------------------------------------------- plumbing

    def _evaluate(self) -> None:
        wanted = (self._want > 0 and self._pause_depth == 0
                  and not getattr(self.app, "_closing", False)
                  and bool(self.app.device.connected))
        if wanted and not self._active:
            self._start()
        elif not wanted and self._active:
            self._stop()

    def _start(self) -> None:
        self._cancel_retry()
        # Fresh state *before* the stream starts: the first frame (the pin
        # snapshot) can arrive on the reader thread before start_stream even
        # returns, and it must land in the merger that is kept, not in one
        # about to be replaced. Whether evdev is the button/axis source is
        # only known once the stream is up, so that flag is set afterwards.
        with self._lock:
            self._merger = StateMerger()
            self._pending = None
            self._pending_changed = set()
            self._delivery_scheduled = False
        now = time.monotonic()
        self._last_frame = now
        self._last_rearm = now
        self._active = True
        try:
            self.app.device.start_stream(
                self._on_frame, interval_ms=STREAM_INTERVAL_MS,
                on_end=self._on_stream_died,
            )
        except Exception as e:
            self._active = False
            log.warning("live monitor failed to start: %s", e)
            self.app.show_status(f"Live monitor unavailable: {e}", "error")
            self._schedule_retry()
            return
        with self._lock:
            self._merger.evdev_active = bool(getattr(self.app.device, "stream_uses_evdev", False))
        self._schedule_watchdog()

    def _stop(self) -> None:
        self._active = False
        self._cancel_watchdog()
        self._cancel_retry()
        try:
            self.app.device.stop_stream()
        except Exception as e:
            log.warning("live monitor failed to stop cleanly: %s", e)
        with self._lock:
            self._pending = None
            self._pending_changed = set()
            # A delivery queued just before the stop would otherwise stay
            # "scheduled" forever and mute the next stream.
            self._delivery_scheduled = False

    # ---------------------------------------------------------------- frames

    def _on_frame(self, frame: dict) -> None:
        """Reader-thread callback. Merges immediately (delta pin frames cannot
        be dropped) but coalesces UI delivery to one queued update."""
        with self._lock:
            state = self._merger.feed(frame)
            self._pending_changed |= state["changed"]
            state["changed"] = set(self._pending_changed)
            self._pending = state
            self._last_frame = time.monotonic()
            if self._delivery_scheduled:
                return
            self._delivery_scheduled = True
        self.app.post(self._deliver)

    def _deliver(self) -> None:
        with self._lock:
            self._delivery_scheduled = False
            state = self._pending
            self._pending = None
            self._pending_changed = set()
        if state is None or not self._active:
            return
        for fn in list(self._subs):
            self._call(fn, state)

    @staticmethod
    def _call(fn: Callable[[State], None], state: State) -> None:
        try:
            fn(state)
        except Exception:
            log.exception("live monitor subscriber failed")

    # ------------------------------------------------------------- watchdog

    def _schedule_watchdog(self) -> None:
        self._cancel_watchdog()
        try:
            self._watchdog_id = self.app.after(WATCHDOG_TICK_MS, self._watchdog_tick)
        except RuntimeError:
            self._watchdog_id = None

    def _cancel_watchdog(self) -> None:
        if self._watchdog_id is not None:
            try:
                self.app.after_cancel(self._watchdog_id)
            except Exception:
                pass
            self._watchdog_id = None

    def _watchdog_tick(self) -> None:
        self._watchdog_id = None
        if not self._active:
            return
        if should_rearm(time.monotonic(), self._last_frame, self._last_rearm):
            self._last_rearm = time.monotonic()
            # Off the UI thread: rearm_stream writes to the serial port.
            threading.Thread(target=self._rearm, daemon=True).start()
        self._schedule_watchdog()

    def _rearm(self) -> None:
        try:
            self.app.device.rearm_stream()
        except Exception as e:
            log.warning("stream re-arm failed: %s", e)

    # --------------------------------------------------------------- restart

    def _schedule_retry(self) -> None:
        self._cancel_retry()
        try:
            self._retry_id = self.app.after(RETRY_MS, self._retry)
        except RuntimeError:
            self._retry_id = None

    def _cancel_retry(self) -> None:
        if self._retry_id is not None:
            try:
                self.app.after_cancel(self._retry_id)
            except Exception:
                pass
            self._retry_id = None

    def _retry(self) -> None:
        self._retry_id = None
        self._evaluate()

    def _on_stream_died(self) -> None:
        """A reader thread exited unexpectedly (device vanished, evdev node
        closed). Reset so the monitor can restart instead of freezing."""
        def apply():
            if not self._active:
                return
            self._active = False
            self._cancel_watchdog()
            if self._want > 0 and self.app.device.connected:
                self.app.show_status("Live monitor stopped — restarting…", "info")
                # Delayed, not immediate: a dead device would otherwise spin
                # start/die cycles as fast as the event loop allows.
                self._schedule_retry()
        self.app.post(apply)

    def _on_connection(self, connected: bool) -> None:
        if not connected:
            self._active = False
            self._cancel_watchdog()
            self._cancel_retry()
            with self._lock:
                self._merger = StateMerger()
                self._pending = None
                self._pending_changed = set()
        self._evaluate()
