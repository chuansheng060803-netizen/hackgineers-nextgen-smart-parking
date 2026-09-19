"""Asks the simulator for its current state (REST GET) on a timer.

Every answer goes to the Store, which writes only what changed. This is the only
place that polls the simulator; nothing else has to, and the dashboard never does.

How often each list is read (seconds to WAIT after the previous read finished,
so a slow simulator is never asked faster than it can answer):

    list-parking-spots  1     list-zones   2      status  5
    list-barriers       1     list-lights  5      list-alarms 5

Change them with the POLL_INTERVALS environment variable, for example
    POLL_INTERVALS="list-parking-spots=0.5,list-zones=1"

A failed read is recorded (poll_status) so stale data shows up as stale, and that
endpoint backs off (1 s, 2 s, 4 s, then 5 s) instead of hammering a struggling
simulator.
"""
import logging
import os
import threading
import time

from store import STATE_TABLES

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/"
DEFAULT_INTERVALS = {
    "list-parking-spots": 1.0,
    "list-barriers": 1.0,
    "list-zones": 2.0,
    "list-lights": 5.0,
    "list-alarms": 5.0,
    "status": 5.0,
}
MAX_BACKOFF_S = 5.0
SLOW_CALL_S = 1.0        # a single read slower than this is logged
IDLE_MAX_S = 0.25        # the loop looks at the clock at least this often


def intervals_from_env(value=None):
    """Parse "list-zones=1,status=10" into {"list-zones": 1.0, "status": 10.0}."""
    text = os.getenv("POLL_INTERVALS", "") if value is None else value
    result = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        name, _, seconds = part.partition("=")
        name = name.strip()
        try:
            seconds = float(seconds)
        except ValueError:
            logger.warning("POLL_INTERVALS: bad number in %r", part)
            continue
        if name in STATE_TABLES and seconds > 0:
            result[name] = seconds
        else:
            logger.warning("POLL_INTERVALS: ignored %r", part)
    return result


class Poller:
    def __init__(self, client, store, intervals=None, clock=time.monotonic):
        self.client = client
        self.store = store
        self.clock = clock
        wanted = {**DEFAULT_INTERVALS, **(intervals or {})}
        self.intervals = {e: s for e, s in wanted.items() if e in STATE_TABLES}
        self._next_due = {e: 0.0 for e in self.intervals}     # 0 = read at once on start
        self._failures = {e: 0 for e in self.intervals}
        self._stop = threading.Event()
        self._thread = None
        self.reads = 0

    def poll_once(self, endpoint):
        """Read one endpoint and hand the answer to the store. True if it worked."""
        started = self.clock()
        try:
            data = self.client.call("GET", API_PREFIX + endpoint)
        except Exception as exc:               # SimulatorError, network, anything
            ms = int((self.clock() - started) * 1000)
            self._failures[endpoint] += 1
            if self._failures[endpoint] in (1, 10, 100):
                logger.warning("Reading %s failed (%d in a row): %s",
                               endpoint, self._failures[endpoint], exc)
            self.store.submit_poll_error(endpoint, exc, ms)
            return False
        seconds = self.clock() - started
        if seconds >= SLOW_CALL_S:
            logger.warning("SLOW read %s took %.1fs", endpoint, seconds)
        self._failures[endpoint] = 0
        self.reads += 1
        try:
            self.store.submit_state(endpoint, data, int(seconds * 1000))
        except Exception:
            logger.exception("Could not hand %s to the store", endpoint)
        return True

    def _delay_after(self, endpoint, ok):
        base = self.intervals[endpoint]
        if ok:
            return base
        exponent = min(self._failures[endpoint] - 1, 10)      # capped: 2 ** 1100 would overflow a float
        return min(MAX_BACKOFF_S, max(base, 1.0) * 2 ** exponent)

    def run_due(self):
        """Read every endpoint whose turn has come. Returns the endpoints read."""
        done = []
        for endpoint in self.intervals:
            if self._stop.is_set():
                break
            if self.clock() >= self._next_due[endpoint]:
                ok = False
                try:
                    ok = self.poll_once(endpoint)
                finally:
                    # The wait starts when the read FINISHED, not when it began; and
                    # whatever happened, this endpoint always gets a next turn.
                    self._next_due[endpoint] = self.clock() + self._delay_after(endpoint, ok)
                done.append(endpoint)
        return done

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("Poller round failed; carrying on")
            soonest = min(self._next_due.values()) - self.clock()
            self._stop.wait(max(0.02, min(soonest, IDLE_MAX_S)))

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="sim-poller", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
