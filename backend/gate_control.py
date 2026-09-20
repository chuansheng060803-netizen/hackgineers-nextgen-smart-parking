"""Manual gate control: an operator's order is the final word.

Every barrier gate has a mode:

    auto            the car logic opens and closes it (normal)
    forced_open     held open by an operator
    forced_closed   held closed by an operator

While a gate is forced, the car logic can no longer move it: its open/close commands
pass through this controller (ControlledClient), which drops them instead of sending.
The controller is therefore the ONLY place that decides whether a gate command reaches
the simulator. A slow background check (enforce_once) also puts a forced gate back if
something else changed it.

The one exception, from the organisers' rule: a broken or under-maintenance component
is never operated. An operator cannot force such a gate, and a gate that breaks while
forced is left alone until it is repaired (then the order is applied again).

Modes are saved in the database (gate_control) so a restart keeps them, and every
manual command is logged (control_log): who, what, and whether it was done or refused.
"""
import itertools
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from simulator_api import SimulatorError

logger = logging.getLogger(__name__)

MODES = ("auto", "forced_open", "forced_closed")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TARGET_STATES = {"forced_open": ("open", "opening"), "forced_closed": ("closed", "closing")}
LABELS = {"auto": "automatic", "forced_open": "held open", "forced_closed": "held closed"}


class GateRefused(Exception):
    """A control request that was not carried out. `status` is the matching HTTP code."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class GateController:
    def __init__(self, client, store=None, gates=(), clock=datetime.now, enforce_every_s=3.0):
        self.client = client                     # its own simulator client, used only for gate commands
        self.store = store                       # saves modes and the log (None in tests)
        self.gates = tuple(gates)                # gates we know even before the barrier list is read
        self._clock = clock
        self.enforce_every_s = enforce_every_s
        self._lock = threading.RLock()
        self._modes = {}                         # gate -> {"mode", "set_by", "set_at"}
        self._held_back = {}                     # gate -> automatic commands dropped since the order
        self._log_ids = itertools.count(int(time.time() * 1000))
        self._stop = threading.Event()
        self._thread = None

    # ---- modes ------------------------------------------------------------

    def _now(self):
        return self._clock().strftime(TIME_FORMAT)

    def load(self, db_path):
        """Read the saved modes (after a restart). A missing table or file means all auto."""
        try:
            conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return
        try:
            rows = conn.execute("SELECT gate, mode, set_by, set_at FROM gate_control").fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        with self._lock:
            for gate, mode, set_by, set_at in rows:
                if mode in MODES:
                    self._modes[gate] = {"mode": mode, "set_by": set_by, "set_at": set_at}
        if rows:
            logger.info("Gate orders restored: %s", {g: m["mode"] for g, m in self._modes.items()})

    def mode_of(self, gate):
        with self._lock:
            return self._modes.get(gate, {}).get("mode", "auto")

    def status(self):
        with self._lock:
            names = sorted(set(self.gates) | set(self._modes))
            return {"gates": {g: {"mode": self.mode_of(g),
                                  "set_by": self._modes.get(g, {}).get("set_by"),
                                  "set_at": self._modes.get(g, {}).get("set_at"),
                                  "held_back": self._held_back.get(g, 0)} for g in names},
                    "enforcing": bool(self._thread and self._thread.is_alive())}

    # ---- the car logic's gate commands go through here ----------------------

    def wrap(self, flow_client):
        """The client to give CarFlow: same as flow_client, but gate commands are checked here."""
        return ControlledClient(flow_client, self)

    def automation_command(self, gate, action, send):
        """The car logic wants `action` ("open"/"close") on `gate`; `send(gate)` really does it."""
        with self._lock:
            mode = self.mode_of(gate)
            if mode == "auto":
                return send(gate)
            if (mode, action) in (("forced_open", "open"), ("forced_closed", "close")):
                return None                      # the operator already wants exactly this
            self._held_back[gate] = self._held_back.get(gate, 0) + 1
            logger.info("%s %s dropped: %s by an operator", gate, action, LABELS[mode])
            if action == "open":                 # the car logic must know the gate is NOT opening
                raise SimulatorError(f"{gate} is held closed by an operator")
            return None                          # a close while held open: nothing to do, nothing to say

    # ---- an operator's order ------------------------------------------------

    def set_mode(self, gate, mode, username, role):
        """Apply an operator's order. Returns what happened; raises GateRefused if not done.

        Refusals are logged too, so the log shows who tried what.
        """
        gate = (gate or "").strip()
        try:
            if role != "operator":
                raise GateRefused("Only operators can control gates.", 403)
            if mode not in MODES:
                raise GateRefused(f"Unknown mode {mode!r}; use one of {', '.join(MODES)}.", 400)
            barrier = self._find_barrier(gate)
            if barrier is None and gate not in self.gates:
                raise GateRefused(f"Unknown gate {gate!r}.", 404)
            if mode != "auto":
                if barrier is None:
                    raise GateRefused(f"Cannot read the state of {gate} right now, so it is left alone.")
                if barrier.get("broken") or barrier.get("isUnderMaintenance"):
                    why = "broken" if barrier.get("broken") else "under maintenance"
                    raise GateRefused(f"{gate} is {why}. A gate in that state is never operated.")
            with self._lock:
                if mode != "auto" and not self._already(barrier, mode):
                    self._send(gate, "open" if mode == "forced_open" else "close")
                self._modes[gate] = {"mode": mode, "set_by": username, "set_at": self._now()}
                self._held_back[gate] = 0
            self._save(gate, mode, username, role, "done", None)
            logger.warning("Gate %s is now %s (set by %s)", gate, LABELS[mode], username)
            return {"gate": gate, "mode": mode, "set_by": username}
        except GateRefused as refusal:
            self._save(gate, mode, username, role, "refused", str(refusal), update_mode=False)
            raise

    def request_repair(self, gate, username, role, confirm_healthy=False):
        """An operator asks for a gate to be repaired / sent to maintenance.

        A broken gate: allowed. A working gate: only with confirm_healthy (it stops the gate
        working until the repair is done). A gate already under maintenance: refused, the
        rule is never to touch a component in that state. Any order to hold the gate
        open/closed stays saved and is applied again once the gate is healthy.
        """
        gate = (gate or "").strip()
        try:
            if role != "operator":
                raise GateRefused("Only operators can request repairs.", 403)
            barrier = self._find_barrier(gate)
            if barrier is None:
                if gate in self.gates:
                    raise GateRefused(f"Cannot read the state of {gate} right now, so it is left alone.")
                raise GateRefused(f"Unknown gate {gate!r}.", 404)
            if barrier.get("isUnderMaintenance"):
                raise GateRefused(f"{gate} is already under maintenance.")
            if not barrier.get("broken") and not confirm_healthy:
                raise GateRefused(f"{gate} is working. Repairing it takes it out of use until the "
                                  "repair is done; confirm to go ahead.")
            with self._lock:
                try:
                    self.client.repair_gate(gate)
                except (SimulatorError, AttributeError) as e:
                    raise GateRefused(f"The simulator did not accept the repair request for {gate}: {e}. "
                                      "Nothing was changed.", 502)
            self._save(gate, "repair", username, role, "done",
                       "broken gate" if barrier.get("broken") else "working gate sent to maintenance")
            logger.warning("Repair requested for %s by %s", gate, username)
            return {"gate": gate, "requested": "repair", "by": username}
        except GateRefused as refusal:
            self._save(gate, "repair", username, role, "refused", str(refusal), update_mode=False)
            raise

    @staticmethod
    def _already(barrier, mode):
        state = str((barrier or {}).get("state", "")).lower()
        return state in TARGET_STATES[mode]

    def _send(self, gate, action):
        try:
            (self.client.open_gate if action == "open" else self.client.close_gate)(gate)
        except (SimulatorError, AttributeError) as e:
            raise GateRefused(f"The simulator did not accept the {action} command for {gate}: {e}. "
                              "Nothing was changed.", 502)

    def _find_barrier(self, gate):
        """The simulator's current record for this gate, or None if it cannot be read/found."""
        try:
            for barrier in self.client.list_barriers() or []:
                if isinstance(barrier, dict) and barrier.get("name") == gate:
                    return barrier
        except (SimulatorError, AttributeError) as e:
            logger.warning("Cannot read barriers for gate control: %s", e)
        return None

    # ---- keeping a forced gate the way the operator left it -----------------

    def enforce_once(self):
        """Put every forced gate back to what it was ordered to be. Returns the gates corrected."""
        fixed = []
        with self._lock:
            forced = {g: m["mode"] for g, m in self._modes.items() if m["mode"] != "auto"}
        for gate, mode in forced.items():
            barrier = self._find_barrier(gate)
            if barrier is None or barrier.get("broken") or barrier.get("isUnderMaintenance"):
                continue                         # never touch what is broken or unreadable
            if self._already(barrier, mode):
                continue
            with self._lock:
                if self.mode_of(gate) != mode:   # changed while we were looking
                    continue
                try:
                    self._send(gate, "open" if mode == "forced_open" else "close")
                except GateRefused as e:
                    logger.error("Could not restore %s: %s", gate, e)
                    continue
            self._save(gate, mode, "system", None, "done",
                       f"{gate} was {barrier.get('state')}; put back to {LABELS[mode]}", update_mode=False)
            fixed.append(gate)
        return fixed

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gate-enforcer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.wait(self.enforce_every_s):
            try:
                self.enforce_once()
            except Exception:
                logger.exception("Gate enforcement check failed")

    # ---- saving ------------------------------------------------------------

    def _save(self, gate, mode, username, role, result, detail, update_mode=True):
        if self.store is None:
            return
        now = self._now()
        if update_mode and result == "done":
            self.store.submit_record("gate_control", {"gate": gate, "mode": mode,
                                                      "set_by": username, "set_at": now})
        self.store.submit_record("control_log", {
            "log_id": next(self._log_ids), "at": now, "username": username or "?", "role": role,
            "gate": gate or "?", "mode": str(mode), "result": result, "detail": detail})


class ControlledClient:
    """CarFlow's client: every call goes to the real client, except gate commands, which the
    GateController may drop."""

    def __init__(self, inner, controller):
        self._inner = inner
        self._controller = controller

    def open_gate(self, name):
        return self._controller.automation_command(name, "open", self._inner.open_gate)

    def close_gate(self, name):
        return self._controller.automation_command(name, "close", self._inner.close_gate)

    def __getattr__(self, attr):
        return getattr(self._inner, attr)
