"""Tiny call timer, so "which call is slow?" is answered by data, not guessing.

    stats = CallStats()
    client = Timed(SimulatorClient(), "simulator", stats)
    client.move_car(...)            # works exactly as before, and is timed
    stats.summary()                 # {"simulator.move_car": {"n": .., "avg_s": .., "max_s": ..}}

Only the last WINDOW calls of each kind are kept, so the numbers describe what
is happening now, not the whole run.
"""
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

WINDOW = 50
SLOW_S = 1.0        # a single call slower than this is logged as a warning


class CallStats:
    def __init__(self):
        self._lock = threading.Lock()
        self._recent = {}       # name -> deque of seconds
        self._count = {}        # name -> total calls ever

    def record(self, name, seconds):
        with self._lock:
            self._recent.setdefault(name, deque(maxlen=WINDOW)).append(seconds)
            self._count[name] = self._count.get(name, 0) + 1
        if seconds >= SLOW_S:
            logger.warning("SLOW call %s took %.1fs", name, seconds)

    def summary(self):
        with self._lock:
            out = {}
            for name, values in self._recent.items():
                out[name] = {
                    "n": self._count[name],
                    "avg_s": round(sum(values) / len(values), 3),
                    "max_s": round(max(values), 3),
                }
            return out


class Timed:
    """Wraps any object; every method call on it is timed under "<label>.<method>"."""

    def __init__(self, target, label, stats):
        self._target = target
        self._label = label
        self._stats = stats

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr
        key = f"{self._label}.{name}"

        def timed(*args, **kwargs):
            start = time.monotonic()
            try:
                return attr(*args, **kwargs)
            finally:
                self._stats.record(key, time.monotonic() - start)

        return timed
