"""Periodic, observational progress; no inferred percentages or completion times."""
from __future__ import annotations

import threading
import time


class ProgressReporter:
    def __init__(self, operation, *, interval=10.0, emit=None):
        self.operation = operation
        self.interval = interval
        self.emit = emit or (lambda text: print(text, flush=True))
        self.phase = "starting"
        self.current = ""
        self.counts = {}
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def update(self, phase=None, current=None, **counts):
        with self._lock:
            changed = phase is not None and phase != self.phase
            if phase is not None:
                self.phase = phase
            if current is not None:
                self.current = str(current)
            self.counts.update(counts)
        if changed:
            self.report()

    def advance(self, **counts):
        with self._lock:
            for key, value in counts.items():
                self.counts[key] = self.counts.get(key, 0) + value

    def report(self, status="working"):
        with self._lock:
            phase, current, counts = self.phase, self.current, dict(self.counts)
        # A heartbeat states liveness, not that a blocked read has advanced.
        details = " | ".join(f"{key}={value:,}" for key, value in counts.items())
        self.emit(f"[{self.operation}] {status} | elapsed={time.monotonic()-self.started:.0f}s"
                  f" | phase={phase}" + (f" | {details}" if details else "")
                  + (f" | current={current}" if current else ""))

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            self.report("still running; completion time unknown")

    def __enter__(self):
        self.report()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.report("finished" if exc_type is None else "interrupted" if exc_type is KeyboardInterrupt else "failed")
        return False
