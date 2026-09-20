"""A client that reads for real but only LOGS commands (used when a backend runs without --live)."""
import logging

logger = logging.getLogger("dry_run")


class DryRunClient:
    """Real read-only calls; commands are logged instead of sent."""

    def __init__(self, real):
        self._real = real

    def list_parking_spots(self):
        return self._real.list_parking_spots()

    def list_barriers(self):
        return self._real.list_barriers()

    def move_car(self, name, destination):
        logger.info("[DRY RUN] would move_car(%r, %r)", name, destination)

    def charge_car(self, name, parking_cost, charging_cost):
        logger.info("[DRY RUN] would charge_car(%r, %r, %r)", name, parking_cost, charging_cost)

    def open_gate(self, name):
        logger.info("[DRY RUN] would open_gate(%r)", name)

    def close_gate(self, name):
        logger.info("[DRY RUN] would close_gate(%r)", name)

    def repair_gate(self, name):
        logger.info("[DRY RUN] would repair_gate(%r)", name)
