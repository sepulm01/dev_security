import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class CentralAPIClient:
    def __init__(self, base_url, slug, token, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.slug = slug
        self.token = token
        self.timeout = timeout

    def _headers(self, body_bytes: bytes):
        timestamp = str(int(time.time()))
        payload = f"{self.slug}.{timestamp}.".encode() + body_bytes
        signature = hmac.new(self.token.encode(), payload, hashlib.sha256).hexdigest()
        return {
            "X-Node-Signature": signature,
            "X-Node-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    def send_telemetry(self, payload: dict) -> dict:
        body = json.dumps(payload, default=str).encode()
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/nodos/{self.slug}/telemetria",
                data=body,
                headers=self._headers(body),
                timeout=self.timeout,
            )
            return {"status_code": response.status_code, "data": _json_or_none(response)}
        except requests.RequestException as e:
            logger.warning("telemetría falló: %s", e)
            return {"status_code": 0, "error": str(e)}

    def poll_commands(self) -> list:
        body = b""
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/nodos/{self.slug}/comandos",
                headers=self._headers(body),
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return (response.json() or {}).get("commands", [])
        except requests.RequestException as e:
            logger.warning("poll de comandos falló: %s", e)
        return []

    def send_command_result(self, command_id: int, success: bool, result: dict) -> None:
        body = json.dumps({"success": success, "result": result}, default=str).encode()
        try:
            requests.post(
                f"{self.base_url}/api/v1/nodos/{self.slug}/comandos/{command_id}/resultado",
                data=body,
                headers=self._headers(body),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning("reporte de comando falló: %s", e)


def _json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None
