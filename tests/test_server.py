"""Integration checks against a real temporary loopback HTTP server."""
import base64
import http.client
import json
import threading
import unittest

import app


POST_TEXT = "我独立记录了三次发布实验的加载时间，并比较不同设备上的结果。这里说明测试过程、失败原因和实际观察到的差异，供读者复核与继续测试。"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.LocalServer(("127.0.0.1", 0))
        cls.worker = threading.Thread(
            target=cls.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        if cls.worker.is_alive():
            raise AssertionError("Temporary local server did not stop")

    def request(self, method, path, value=None, headers=None, raw=None, server=None):
        target = server or self.server
        request_headers = {}
        if method == "POST":
            request_headers = {
                "Origin": target.origin,
                "X-Local-Token": target.token,
                "Content-Type": "application/json",
            }
        if headers:
            request_headers.update(headers)
        body = raw
        if body is None and value is not None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", target.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, value=None, **kwargs):
        status, headers, body = self.request(method, path, value, **kwargs)
        self.assertIn("application/json", headers["Content-Type"])
        return status, json.loads(body.decode("utf-8"))

    @staticmethod
    def import_payload(project):
        content = json.dumps(project, ensure_ascii=False).encode("utf-8")
        return {"filename": "project.json", "content_base64": base64.b64encode(content).decode("ascii")}

    def test_static_gui_and_health_are_served_with_local_headers(self):
        for path, expected_type, marker in (
            ("/", "text/html", "拖入 X 归档 ZIP".encode("utf-8")),
            ("/styles.css", "text/css", b".page-shell"),
            ("/app.js", "text/javascript", b"inspectArchive"),
        ):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertIn(expected_type, headers["Content-Type"])
                self.assertIn(marker, body)
                self.assertEqual("no-store", headers["Cache-Control"])
                self.assertEqual("nosniff", headers["X-Content-Type-Options"])
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, health = self.json_request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])
        self.assertEqual("x-originality-checker", health["app"])
        self.assertEqual(self.server.token, health["token"])
        self.assertGreaterEqual(len(health["token"]), 32)

    def test_import_analyze_report_and_saved_selection_roundtrip(self):
        original = {
            "account": "@local_test",
            "scope": {"selection": "sample", "complete": False},
            "posts": [{"id": "P1", "text": POST_TEXT, "created_at": "2026-10-06T10:00:00+08:00"}],
            "selected_candidates": ["P1"],
        }
        status, imported = self.json_request("POST", "/api/import", self.import_payload(original))
        self.assertEqual(200, status)
        self.assertEqual(["P1"], imported["project"]["selected_candidates"])
        status, assessed = self.json_request("POST", "/api/analyze", {"project": imported["project"]})
        self.assertEqual(200, status)
        self.assertEqual(1, assessed["analysis"]["summary"]["total"])
        self.assertEqual(["P1"], assessed["project"]["selected_candidates"])
        # This is the same JSON representation downloaded by Save Project and re-imported.
        status, restored = self.json_request("POST", "/api/import", self.import_payload(assessed["project"]))
        self.assertEqual(200, status)
        self.assertEqual(assessed["project"], restored["project"])
        for report_format in ("markdown", "html"):
            with self.subTest(format=report_format):
                status, report = self.json_request("POST", "/api/report", {"project": restored["project"], "format": report_format})
                self.assertEqual(200, status)
                self.assertEqual(report_format, report["format"])
                self.assertIn(POST_TEXT, report["content"])
                self.assertIn("P1", report["content"])
                self.assertIn("用户已选", report["content"])

    def test_wrong_origin_token_and_host_are_rejected(self):
        for headers in (
            {"Origin": "https://untrusted.example"},
            {"Origin": ""},
            {"X-Local-Token": "wrong-token"},
            {"X-Local-Token": ""},
            {"Host": "untrusted.example"},
        ):
            with self.subTest(headers=list(headers)):
                status, rejected = self.json_request("POST", "/api/analyze", {"project": {}}, headers=headers)
                self.assertEqual(403, status)
                self.assertIn("error", rejected)
        status, rejected = self.json_request("GET", "/api/health", headers={"Host": "untrusted.example"})
        self.assertEqual(403, status)
        self.assertNotIn("token", rejected)
        status, accepted = self.json_request("POST", "/api/analyze", {"project": {"posts": []}}, headers={
            "Origin": f"http://localhost:{self.server.server_port}",
            "Host": f"localhost:{self.server.server_port}",
        })
        self.assertEqual(200, status)
        self.assertEqual(0, accepted["analysis"]["summary"]["total"])

    def test_unknown_routes_cannot_read_files_or_process_material(self):
        for path in ("/unknown", "/../app.py", "/api/unknown"):
            with self.subTest(path=path):
                status, value = self.json_request("GET", path)
                self.assertEqual(404, status)
                self.assertIn("error", value)
        status, value = self.json_request("POST", "/api/unknown", {"project": {}})
        self.assertEqual(404, status)
        self.assertIn("error", value)

    def test_invalid_request_json_and_import_decode_have_visible_errors(self):
        for raw in (b"{broken", b"[]", b"\xff", b"null"):
            with self.subTest(raw=repr(raw)):
                status, value = self.json_request("POST", "/api/import", raw=raw)
                self.assertEqual(400, status)
                self.assertIn("error", value)
        for payload in (
            {"filename": "posts.json", "content_base64": "***invalid***"},
            {"filename": "posts.json", "content_base64": base64.b64encode(b"{broken").decode()},
            {"filename": "posts.json", "content_base64": base64.b64encode(b"\xff").decode()},
        ):
            with self.subTest(payload=payload["content_base64"]):
                status, value = self.json_request("POST", "/api/import", payload)
                self.assertEqual(400, status)
                self.assertIn("error", value)
        status, value = self.json_request("POST", "/api/import", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(415, status)
        self.assertIn("error", value)

    def test_api_scope_filters_real_post_dates_in_declared_timezone(self):
        project = {"scope": {"start": "2026-10-06", "end": "2026-10-06", "timezone": "Asia/Shanghai"}, "posts": [
            {"id": "before", "text": "前一天的内容", "created_at": "2026-10-05T15:59:59Z"},
            {"id": "start", "text": POST_TEXT, "created_at": "2026-10-05T16:00:00Z"},
            {"id": "end", "text": "我自行记录了这个研究过程中的条件和观察结果，整理全部测量数据并解释实验的局限，供后续读者核对。", "created_at": "2026-10-06T15:59:59Z"},
            {"id": "after", "text": "后一天的内容", "created_at": "2026-10-06T16:00:00Z"},
            {"id": "unknown", "text": "时间未知的内容"},
        ]}
        status, value = self.json_request("POST", "/api/analyze", {"project": project})
        self.assertEqual(200, status)
        result = value["analysis"]
        self.assertEqual(2, result["summary"]["total"])
        self.assertEqual({"start", "end"}, {post["id"] for post in result["posts"]})
        self.assertEqual({"before", "after"}, set(result["coverage"]["outside_scope"]))
        self.assertEqual(["unknown"], result["coverage"]["invalid_date"])
        self.assertEqual(5, len(value["project"]["posts"]), "Save data must retain out-of-range posts")
        status, value = self.json_request("POST", "/api/analyze", {"project": {"scope": {"start": "tomorrow"}, "posts": []}})
        self.assertEqual(400, status)
        self.assertIn("error", value)

    def test_html_report_escapes_user_script_and_markup(self):
        attack = '<script>alert("injected")</script><img src=x onerror=alert(1)>'
        project = {"account": attack, "posts": [{"id": "P-safe", "text": POST_TEXT + attack}], "selected_candidates": ["P-safe"]}
        status, report = self.json_request("POST", "/api/report", {"project": project, "format": "html"})
        self.assertEqual(200, status)
        content = report["content"]
        self.assertNotIn("<script", content.lower())
        self.assertNotIn("<img", content.lower())
        self.assertIn("&lt;script&gt;alert(&quot;injected&quot;)&lt;/script&gt;", content)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", content)
        self.assertIn("用户已选：P-safe", content)

    def test_shutdown_requires_current_local_session_and_stops_instance(self):
        # A separate instance allows this lifecycle test without stopping other tests.
        server = app.LocalServer(("127.0.0.1", 0))
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            status, value = self.json_request("POST", "/api/shutdown", {}, headers={"X-Local-Token": "wrong"}, server=server)
            self.assertEqual(403, status)
            self.assertTrue(worker.is_alive())
            status, value = self.json_request("POST", "/api/shutdown", {}, server=server)
            self.assertEqual(200, status)
            self.assertTrue(value["ok"])
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
