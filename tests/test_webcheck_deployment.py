"""Real host-process deployment probes, without provider API calls or Docker."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "deployment" / "container_entrypoint.py"
HEALTHCHECK = ROOT / "deployment" / "container_healthcheck.py"
TEST_TOKEN = "fictional-service-access-token-for-local-tests-only"
TEST_PROVIDER_KEY = "fictional-provider-key-never-used-in-a-search"


def spare_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_environment(port, configured=False):
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PORT": str(port),
        "WEBCHECK_PROVIDER": "tavily",
        "TAVILY_API_KEY": TEST_PROVIDER_KEY if configured else "",
        "BRAVE_SEARCH_API_KEY": "",
        "WEBCHECK_ACCESS_TOKEN": TEST_TOKEN,
        "WEBCHECK_ALLOWED_HOSTS": "probe.example.invalid",
        "WEBCHECK_ALLOWED_ORIGINS": "https://potervlag-cyber.github.io",
        "WEBCHECK_HOURLY_QUERY_BUDGET": "100",
    })
    return env


def request(port, method="GET", path="/api/webcheck/status", body=None, host="probe.example.invalid"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        headers = {"Host": host, "Authorization": "Bearer " + TEST_TOKEN}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class ContainerDeploymentTests(unittest.TestCase):
    def test_docker_copy_sources_are_explicitly_allowed_without_widening_context(self):
        import shlex
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8-sig")
        patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual("*", patterns[0])
        sources = []
        for line in dockerfile.splitlines():
            if line.lstrip().upper().startswith("COPY "):
                tokens = shlex.split(line)
                sources.extend(token for token in tokens[1:-1] if not token.startswith("--"))
        self.assertEqual({"webcheck.py", "webcheck_server.py", "content_review.py",
                          "deployment/container_entrypoint.py", "deployment/container_healthcheck.py"}, set(sources))
        for source in sources:
            self.assertTrue((ROOT / source).is_file(), source)
            self.assertIn("!" + source, patterns, f"Docker COPY source is absent from the build-context allowlist: {source}")
        self.assertEqual({"!Dockerfile", "!.dockerignore", "!deployment/", *("!" + source for source in sources)},
                         {pattern for pattern in patterns if pattern.startswith("!")})
        self.assertIn("deployment/*", patterns)

    def launch(self, configured=False):
        port = spare_port()
        env = test_environment(port, configured)
        process = subprocess.Popen([sys.executable, str(ENTRYPOINT)], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8")

        def stop():
            if process.poll() is None:
                process.terminate()
            output, _ = process.communicate(timeout=5)
            self.assertNotIn(TEST_TOKEN, output)
            self.assertNotIn(TEST_PROVIDER_KEY, output)

        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("entrypoint stopped before the local probe became available")
            try:
                status, report = request(port)
                if status == 200:
                    return port, env, report
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.03)
        self.fail("entrypoint did not open its configured port")

    def probe(self, env):
        return subprocess.run([sys.executable, str(HEALTHCHECK)], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=5)

    def test_unconfigured_service_starts_but_is_not_ready_and_query_returns_503(self):
        port, env, report = self.launch()
        self.assertFalse(report["ready"])
        health = self.probe(env)
        self.assertEqual(health.returncode, 1)
        self.assertEqual(health.stdout + health.stderr, "")
        status, result = request(port, "POST", "/api/webcheck", {
            "consent": True,
            "posts": [{"id": "deployment-test", "text": "This public fictional sentence is long enough to exercise explicit unavailable provider handling.",
                       "url": "https://x.com/example/status/123456789", "created_at": "2026-10-07T08:00:00Z"}],
        })
        self.assertEqual(status, 503)
        self.assertEqual(result["code"], "provider_not_configured")

    def test_configured_health_probe_passes_without_searching(self):
        _port, env, report = self.launch(configured=True)
        self.assertTrue(report["ready"])
        self.assertEqual(report["limits"]["hourly_queries_remaining"], 100)
        health = self.probe(env)
        self.assertEqual(health.returncode, 0)
        self.assertEqual(health.stdout + health.stderr, "")

    def test_health_probe_uses_explicit_host_and_does_not_bypass_it(self):
        port, env, _report = self.launch(configured=True)
        status, _result = request(port, host="other.example.invalid")
        self.assertEqual(status, 403)
        env["WEBCHECK_ALLOWED_HOSTS"] = "other.example.invalid"
        self.assertEqual(self.probe(env).returncode, 1)

    def test_missing_public_security_configuration_refuses_startup(self):
        for variable in ("WEBCHECK_ACCESS_TOKEN", "WEBCHECK_ALLOWED_HOSTS", "WEBCHECK_ALLOWED_ORIGINS"):
            with self.subTest(variable=variable):
                env = test_environment(spare_port(), configured=True)
                env[variable] = ""
                result = subprocess.run([sys.executable, str(ENTRYPOINT)], cwd=ROOT, env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TEST_TOKEN, result.stdout + result.stderr)
                self.assertNotIn(TEST_PROVIDER_KEY, result.stdout + result.stderr)

    def test_invalid_platform_ports_fail_without_environment_diagnostics(self):
        for value in ("0", "65536", "not-a-port", "", "８７８７"):
            with self.subTest(port=value):
                env = test_environment(spare_port(), configured=True)
                env["PORT"] = value
                result = subprocess.run([sys.executable, str(ENTRYPOINT)], cwd=ROOT, env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn(TEST_TOKEN, result.stdout + result.stderr)
                self.assertNotIn(TEST_PROVIDER_KEY, result.stdout + result.stderr)

    def test_probe_fails_closed_when_service_is_absent(self):
        result = self.probe(test_environment(spare_port(), configured=True))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout + result.stderr, "")


if __name__ == "__main__":
    unittest.main()
