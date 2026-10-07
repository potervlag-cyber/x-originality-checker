"""Exercise the actual Docker image using empty provider keys and local HTTP.

This is a deployment smoke test, not evidence of successful public-web search.
Only the exact container/image created here are removed in the final cleanup.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PORT = 18787
HOST = "smoke.example.invalid"
ORIGIN = "https://potervlag-cyber.github.io"
# Public fixture credential only. The test never imports real environment keys.
TOKEN = "public-container-smoke-fixture-token-not-a-real-credential"


def docker(*args, expected=0, timeout=30):
    result = subprocess.run(["docker", *args], cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout)
    if expected is not None and result.returncode != expected:
        raise RuntimeError(f"Docker {args[0]} returned {result.returncode}; expected {expected}.")
    return result


def request(method="GET", *, origin=ORIGIN, token=TOKEN, host=HOST):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
    headers = {"Host": host, "Origin": origin, "Authorization": "Bearer " + token}
    body = None
    path = "/api/webcheck/status"
    if method == "POST":
        path = "/api/webcheck"
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "consent": True,
            "posts": [{"id": "public-container-smoke-test", "text": "This is a fictional public smoke-test sentence. No search provider will receive it because its API key is empty.",
                       "url": "https://x.com/example/status/123456789", "created_at": "2026-10-07T08:00:00Z"}],
        }).encode("utf-8")
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        headers = dict(response.getheaders())
        raw = response.read(16385)
        if len(raw) > 16384:
            raise RuntimeError("Smoke-test HTTP response exceeded its limit.")
        return status, headers, json.loads(raw)
    finally:
        connection.close()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for_status(container):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip()
        require(state == "true", "The smoke-test container stopped before becoming reachable.")
        try:
            status, headers, result = request()
            if status == 200:
                return status, headers, result
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    raise RuntimeError("The smoke-test container did not respond on loopback:18787.")


def main():
    suffix = uuid.uuid4().hex[:12]
    image = "originality-webcheck-smoke:" + suffix
    container = "originality-webcheck-smoke-" + suffix
    container_created = False
    image_created = False
    result = None
    failure = None
    cleanup_errors = []
    try:
        docker("build", "--pull", "--tag", image, "--file", "Dockerfile", ".", timeout=300)
        image_created = True
        user = docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
        require(user == "10001:10001", "Image is not configured to run as UID/GID 10001.")
        health_command = json.loads(docker("image", "inspect", "--format", "{{json .Config.Healthcheck.Test}}", image).stdout)
        require(health_command == ["CMD", "python", "deployment/container_healthcheck.py"],
                "Image does not contain the expected readiness health check.")

        # Explicitly empty both provider keys, even if the CI host has secrets.
        # This container cannot issue a paid/external search through the service.
        # Docker may create the container and then fail to start it (e.g. an
        # occupied published port). Cleanup must include that failure case.
        container_created = True
        docker("run", "--detach", "--name", container,
               "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
               "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--memory=256m", "--cpus=1", "--pids-limit=64",
               "--publish", f"127.0.0.1:{PORT}:8787",
               "--env", "PORT=8787", "--env", "WEBCHECK_PROVIDER=tavily",
               "--env", "TAVILY_API_KEY=", "--env", "BRAVE_SEARCH_API_KEY=",
               "--env", "WEBCHECK_ACCESS_TOKEN=" + TOKEN,
               "--env", "WEBCHECK_ALLOWED_HOSTS=" + HOST,
               "--env", "WEBCHECK_ALLOWED_ORIGINS=" + ORIGIN,
               "--env", "WEBCHECK_HOURLY_QUERY_BUDGET=100", image)
        status, headers, report = wait_for_status(container)
        require(status == 200 and report.get("ready") is False, "Missing provider must remain not ready.")
        require(headers.get("Access-Control-Allow-Origin") == ORIGIN, "Allowed browser Origin did not receive CORS access.")
        require(report.get("limits", {}).get("hourly_queries_remaining") == 100, "Health probes spent a query budget.")

        uid = docker("exec", container, "python", "-c", "import os; print(os.getuid())").stdout.strip()
        require(uid == "10001", "Running container has the wrong effective UID.")
        source_files = json.loads(docker("exec", container, "python", "-c",
                                       "import json,os; print(json.dumps({'app': sorted(os.listdir('/app')), 'deployment': sorted(os.listdir('/app/deployment'))}))").stdout)
        require(source_files == {"app": ["deployment", "webcheck.py", "webcheck_server.py"],
                                 "deployment": ["container_entrypoint.py", "container_healthcheck.py"]},
                "Image application directory contains files outside the service allowlist.")

        rejected_status, rejected_headers, _ = request(origin="https://not-allowed.example.invalid")
        require(rejected_status == 403 and "Access-Control-Allow-Origin" not in rejected_headers,
                "Unexpected Origin was not rejected.")
        rejected_status, _, _ = request(host="not-allowed.example.invalid")
        require(rejected_status == 403, "Unexpected Host was not rejected.")
        rejected_status, _, rejected = request("POST", token="wrong-public-fixture-token")
        require(rejected_status == 401 and rejected.get("code") == "unauthorized", "Wrong service token was not rejected.")
        unavailable_status, _, unavailable = request("POST")
        require(unavailable_status == 503 and unavailable.get("code") == "provider_not_configured",
                "Missing provider query did not explicitly return 503.")
        health = docker("exec", container, "python", "deployment/container_healthcheck.py", expected=1)
        require(not health.stdout and not health.stderr, "Health check emitted unexpected diagnostic content.")

        result = {
            "scope": "actual_docker_build_and_local_http_only",
            "provider_keys": "explicitly_empty",
            "public_search_executed": False,
            "image_user": user,
            "effective_uid": int(uid),
            "source_allowlist_verified": True,
            "status_http": status,
            "ready": report["ready"],
            "allowed_origin_cors": True,
            "rejected_origin_http": 403,
            "rejected_host_http": 403,
            "rejected_token_http": 401,
            "unconfigured_query_http": unavailable_status,
            "unconfigured_health_exit": health.returncode,
        }
    except FileNotFoundError:
        failure = "docker_executable_unavailable"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        failure = str(exc)
    finally:
        # Remove only the exact names created in this invocation, no pruning.
        if container_created:
            try:
                exists = docker("container", "inspect", container, expected=None)
                if exists.returncode == 0:
                    docker("rm", "--force", container)
                elif exists.returncode != 1:
                    cleanup_errors.append("container_inspection_failed")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                cleanup_errors.append("container_cleanup_failed")
        if image_created:
            try:
                docker("image", "rm", "--force", image)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                cleanup_errors.append("image_cleanup_failed")
    if failure or cleanup_errors:
        print(json.dumps({"passed": False, "error": failure, "cleanup_errors": cleanup_errors}, ensure_ascii=False))
        return 1
    result.update({"passed": True, "cleanup": "exact_smoke_container_and_image_removed"})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
