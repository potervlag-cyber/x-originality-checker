"""Verify the browser adapter with real import, analysis, and report functions."""
import base64
import hashlib
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "browser"))

import browser_api
from engine import analyze
from importers import normalize_project

TEXT = "我在同一台设备上连续测试了三种发布方式，记录每次加载延迟和失败原因。结果显示完整来源说明减少了后续核实时间，以下是测量过程与具体结论。"


def request(path, data=None):
    return json.loads(browser_api.dispatch_json(json.dumps({"path": path, "data": data or {}}, ensure_ascii=False)))


class BrowserAdapterTests(unittest.TestCase):
    def test_health_contains_version_but_no_local_token(self):
        response = request("/api/health")
        self.assertTrue(response["ok"])
        self.assertEqual("browser", response["result"]["runtime"])
        self.assertNotIn("token", response["result"])

    def test_import_analyze_save_reload_and_reports_keep_same_shape(self):
        project = {"account": "@browser-test", "posts": [{"id": "P1", "text": TEXT, "created_at": "2026-10-06T09:00:00+08:00"}], "selected_candidates": ["P1"]}
        imported = request("/api/import", {"filename": "project.json", "content_base64": base64.b64encode(json.dumps(project, ensure_ascii=False).encode()).decode()})
        self.assertTrue(imported["ok"])
        assessed = request("/api/analyze", {"project": imported["result"]["project"]})
        self.assertTrue(assessed["ok"])
        self.assertEqual(analyze(normalize_project(project)), assessed["result"]["analysis"])
        self.assertEqual(["P1"], assessed["result"]["project"]["selected_candidates"])
        saved_json = json.dumps(assessed["result"]["project"], ensure_ascii=False).encode()
        restored = request("/api/import", {"filename": "saved-project.json", "content_base64": base64.b64encode(saved_json).decode()})
        self.assertTrue(restored["ok"])
        self.assertEqual(assessed["result"]["project"], restored["result"]["project"])
        for report_format in ("html", "markdown"):
            with self.subTest(report_format=report_format):
                report = request("/api/report", {"project": restored["result"]["project"], "format": report_format})
                self.assertTrue(report["ok"])
                self.assertEqual(report_format, report["result"]["format"])
                self.assertIn(TEXT, report["result"]["content"])
                self.assertIn("P1", report["result"]["content"])

    def test_utf16_import_and_deflated_zip_with_media_sha256(self):
        encoded = base64.b64encode(json.dumps({"posts": [{"id": "utf16", "text": TEXT}]}, ensure_ascii=False).encode("utf-16")).decode()
        response = request("/api/import", {"filename": "posts.json", "content_base64": encoded})
        self.assertEqual(TEXT, response["result"]["project"]["posts"][0]["text"])
        media = b"binary-local-media"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("posts.json", json.dumps({"posts": [{"id": "zip", "text": TEXT, "media": [{"name": "picture.png"}]}]}))
            archive.writestr("picture.png", media)
            archive.writestr("data/direct-messages.js", "THIS MUST NOT BE READ")
        response = request("/api/import", {"filename": "filtered.zip", "content_base64": base64.b64encode(stream.getvalue()).decode()})
        self.assertTrue(response["ok"])
        self.assertEqual(hashlib.sha256(media).hexdigest(), response["result"]["project"]["posts"][0]["media"][0]["hash"])
        self.assertEqual(["posts.json"], response["result"]["imported_files"])

    def test_bad_requests_and_invalid_base64_are_visible_errors(self):
        for raw in ("[1]", "not-json", json.dumps({"path": "/api/analyze", "data": []}), json.dumps({"path": "/api/unknown", "data": {}})):
            with self.subTest(raw=raw):
                response = json.loads(browser_api.dispatch_json(raw))
                self.assertFalse(response["ok"])
                self.assertTrue(response["error"])
        response = request("/api/import", {"filename": "posts.json", "content_base64": "not*base64"})
        self.assertFalse(response["ok"])

    def test_date_window_and_unsupported_report_format_return_errors(self):
        self.assertFalse(request("/api/analyze", {"project": {"posts": [], "scope": {"timezone": "UTC+30"}}})["ok"])
        self.assertFalse(request("/api/report", {"project": {"posts": []}, "format": "pdf"})["ok"])

    def test_request_and_decoded_file_limits_are_checked(self):
        with patch.object(browser_api, "MAX_REQUEST_BYTES", 10):
            response = request("/api/health")
            self.assertFalse(response["ok"])
            self.assertIn("45 MB", response["error"])
        with patch.object(browser_api, "MAX_FILE_BYTES", 2):
            response = request("/api/import", {"filename": "posts.json", "content_base64": "YWJj"})
            self.assertFalse(response["ok"])
            self.assertIn("30 MB", response["error"])

    def test_internal_failure_does_not_expose_private_exception(self):
        with patch.object(browser_api, "analyze", side_effect=RuntimeError("private secret material")):
            response = request("/api/analyze", {"project": {"posts": []}})
            self.assertFalse(response["ok"])
            self.assertNotIn("private secret", response["error"])

    def test_archive_protocol_keeps_full_posts_in_worker_and_clears_state(self):
        data = {"files": [{"name": "data/tweets.js", "text": 'window.YTD.tweets.part0 = ' + json.dumps([{"tweet": {"id_str": "123456789", "full_text": TEXT}}]) + ';'}], "metadata": {"zip_entries": 20000}}
        prepared = json.loads(browser_api.prepare_archive_json(json.dumps(data)))
        self.assertTrue(prepared["ok"])
        self.assertEqual([], prepared["result"]["media_names"])
        self.assertNotIn("project", prepared["result"])
        finished = json.loads(browser_api.finish_archive_json('{"hashes":[]}'))
        self.assertTrue(finished["ok"])
        self.assertEqual(1, finished["result"]["summary"]["total"])
        self.assertIsNone(finished["result"]["summary"]["official_probability"])
        self.assertIsNone(browser_api._prepared_archive)
        self.assertFalse(json.loads(browser_api.finish_archive_json('{}'))["ok"])

    def test_archive_input_failure_never_leaves_previous_material(self):
        browser_api._prepared_archive = {"old": "old private archive"}
        self.assertFalse(json.loads(browser_api.prepare_archive_json('not json'))["ok"])
        self.assertIsNone(browser_api._prepared_archive)


if __name__ == "__main__":
    unittest.main()
