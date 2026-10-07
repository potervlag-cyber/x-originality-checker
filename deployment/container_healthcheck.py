"""Probe service configuration readiness without sending search queries."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment.container_entrypoint import parse_port


def healthy(settings=None):
    settings = os.environ if settings is None else settings
    connection = None
    try:
        port = parse_port(settings)
        hosts = [value.strip() for value in settings.get("WEBCHECK_ALLOWED_HOSTS", "").split(",") if value.strip()]
        if not hosts:
            return False
        headers = {"Host": hosts[0]}
        token = settings.get("WEBCHECK_ACCESS_TOKEN", "")
        if token:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/webcheck/status", headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(16385)
        if len(body) > 16384:
            return False
        status = json.loads(body)
        return (isinstance(status, dict) and status.get("schema_version") == 1
                and status.get("ok") is True and status.get("ready") is True)
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    # Container health output is intentionally empty: no host, token, key,
    # submitted content, or upstream exception can enter platform logs.
    raise SystemExit(0 if healthy() else 1)
