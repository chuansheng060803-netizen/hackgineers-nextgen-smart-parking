"""Thin client for the (organizer-provided) parking simulator API.

Only documented endpoints are exposed. No business logic here.
"""
import os
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SIMULATOR_BASE_URL", "http://localhost:9898")
EMAIL = os.getenv("SIMULATOR_EMAIL", "admin")
PASSWORD = os.getenv("SIMULATOR_PASSWORD", "admin")
TIMEOUT = 10  # seconds


class SimulatorError(Exception):
    """Any failure talking to the simulator (network, HTTP status, bad JSON)."""


class SimulatorClient:
    def __init__(self, base_url=BASE_URL, email=EMAIL, password=PASSWORD, timeout=TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self.token = None

    def _send(self, method, path, **kwargs):
        try:
            response = requests.request(
                method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise SimulatorError(f"Cannot reach simulator at {self.base_url}: {e}") from e
        return response

    @staticmethod
    def _json(response):
        """Parsed JSON, or None for an empty success body (e.g. gate open/close)."""
        if not response.ok:
            raise SimulatorError(
                f"{response.request.method} {response.url} failed: "
                f"{response.status_code} {response.text}"
            )
        if not response.content.strip():
            return None
        try:
            return response.json()
        except ValueError as e:
            raise SimulatorError(f"Invalid JSON from {response.url}: {response.text}") from e

    def login(self):
        """POST /api/v1/auth/login. Stores and returns the token."""
        response = self._send(
            "POST",
            "/api/v1/auth/login",
            json={"email": self.email, "password": self.password},
        )
        data = self._json(response)
        try:
            self.token = data["token"]
        except (KeyError, TypeError) as e:
            raise SimulatorError(f"Login response has no token: {data}") from e
        return self.token

    def call(self, method, path, **kwargs):
        """Authenticated call to any documented endpoint.

        Returns parsed JSON, or None if the successful response body is empty.
        Logs in on first use and retries once if the token is rejected (401).
        """
        if not self.token:
            self.login()
        extra_headers = kwargs.pop("headers", {})
        for attempt in (1, 2):
            headers = {**extra_headers, "Authorization": f"Bearer {self.token}"}
            response = self._send(method, path, headers=headers, **kwargs)
            if response.status_code == 401 and attempt == 1:
                self.login()
                continue
            return self._json(response)

    def list_parking_spots(self):
        """GET /api/v1/list-parking-spots -> list of spot dicts."""
        return self.call("GET", "/api/v1/list-parking-spots")

    def list_barriers(self):
        """GET /api/v1/list-barriers -> parsed JSON."""
        return self.call("GET", "/api/v1/list-barriers")

    def open_gate(self, name):
        """POST /api/v1/barrier-gates/{name}/open (201, empty body). Returns None."""
        self.call("POST", f"/api/v1/barrier-gates/{quote(name, safe='')}/open")

    def close_gate(self, name):
        """POST /api/v1/barrier-gates/{name}/close (201, empty body). Returns None."""
        self.call("POST", f"/api/v1/barrier-gates/{quote(name, safe='')}/close")

    def move_car(self, name, destination):
        """POST /api/v1/car/{name}/goto/{destination} (201, empty body). Returns None.

        destination: a parking spot name, "exit" or "leavepark".
        Sending a car to an occupied spot may cause a penalty.
        """
        self.call(
            "POST",
            f"/api/v1/car/{quote(name, safe='')}/goto/{quote(destination, safe='')}",
        )
