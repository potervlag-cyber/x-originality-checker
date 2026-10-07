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
from reports import html_report, markdown_report

TEXT = "我在同一台设备上连续测试了三种发布方式，记录每次加载延迟和失败原因。结果显示完整来源说明减少了后续核实时间，以下是测量过程与具体结论。"


def request(path, data=None):
    return json.loads(browser_api.dispatch_json(json.dumps({"path": path, "data": data or {}}, ensure_ascii=False)))


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self):
        browser_api.clear_archive()
        self.addCleanup(browser_api.clear_archive)

    def archive(self, records):
        data = {"files": [{"name": "data/tweets.js", "text": 'window.YTD.tweets.part0 = ' + json.dumps([{"tweet": item} for item in records]) + ';'}]}
        prepared = json.loads(browser_api.prepare_archive_json(json.dumps(data)))
        self.assertTrue(prepared["ok"], prepared)
        finished = json.loads(browser_api.finish_archive_json('{"hashes":[]}'))
        self.assertTrue(finished["ok"], finished)
        return finished["result"]

    def web_archive(self, records):
        # Actual extra public fixture records make strict manual10 possible;
        # never mutate retained eligibility independently of local analysis.
        fillers = [{"id_str": str(900000000 + index), "full_text": TEXT + f"补充公开测试记录{index}"} for index in range(10)]
        return self.archive([*records, *fillers])

    def links(self, ids=None):
        ids = ids or [post["id"] for post in browser_api._retained_archive["eligible"][:10]]
        return [f"https://x.com/example/status/{pid}" for pid in ids]

    def plan(self, data=None):
        payload = {"mode": "manual10", **(data or {})}
        if browser_api._retained_archive is None:
            return request("/api/webcheck/plan", payload)
        if not browser_api._retained_archive["selection_locked"]:
            payload.setdefault("links", self.links())
        return request("/api/webcheck/plan", payload)

    def report(self, post_id, status="no_match", matches=None, **extra):
        return {"schema_version": 1, "provider": "fixture", "checked_at": "2026-10-07T01:00:00Z", "posts": [{
            "id": post_id, "status": status, "query_count": 2, "successful_queries": 0 if status in {"failed", "skipped"} else 2,
            "candidates_found": len(matches or []), "sources_checked": len(matches or []), "matches": matches or [],
            "same_post": [], "issues": [], "max_similarity": max((m["score"] for m in matches or []), default=None), **extra}]}

    def match(self, **extra):
        return {"url": "https://example.com/article", "title": "公开来源", "score": 0.93,
            "matched_chars": 40, "post_excerpt": TEXT[:40], "source_excerpt": TEXT[:40],
            "published_at": "2026-10-01", "published_at_basis": "page_metadata", "temporal_relation": "earlier",
            "source_kind": "page_body", "page_status": "fetched", "source_text_truncated": False, **extra}

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

    def test_archive_protocol_retains_text_for_web_check_but_clears_prepared_state(self):
        data = {"files": [{"name": "data/tweets.js", "text": 'window.YTD.tweets.part0 = ' + json.dumps([{"tweet": {"id_str": "123456789", "full_text": TEXT}}]) + ';'}], "metadata": {"zip_entries": 20000}}
        prepared = json.loads(browser_api.prepare_archive_json(json.dumps(data)))
        self.assertTrue(prepared["ok"])
        self.assertEqual([], prepared["result"]["media_names"])
        self.assertNotIn("project", prepared["result"])
        finished = json.loads(browser_api.finish_archive_json('{"hashes":[]}'))
        self.assertTrue(finished["ok"])
        self.assertEqual(1, finished["result"]["summary"]["total"])
        self.assertIsNone(finished["result"]["summary"]["official_probability"])
        summary = finished["result"]["summary"]
        self.assertIsInstance(summary["estimated_probability"], int)
        self.assertLessEqual(summary["probability_range"]["low"], summary["estimated_probability"])
        self.assertGreaterEqual(summary["probability_range"]["high"], summary["estimated_probability"])
        self.assertFalse(summary["probability_model"]["calibrated"])
        self.assertEqual(summary["estimated_probability"], sum(factor["impact_points"] for factor in finished["result"]["probability_factors"]))
        self.assertIsNone(browser_api._prepared_archive)
        self.assertIsNotNone(browser_api._retained_archive)
        self.assertNotIn("posts", finished["result"])
        self.assertEqual("not_started", finished["result"]["web_check"]["status"])
        self.assertFalse(json.loads(browser_api.finish_archive_json('{}'))["ok"])
        self.assertIsNone(browser_api._retained_archive)

    def test_archive_input_failure_never_leaves_previous_material(self):
        browser_api._prepared_archive = {"old": "old private archive"}
        self.assertFalse(json.loads(browser_api.prepare_archive_json('not json'))["ok"])
        self.assertIsNone(browser_api._prepared_archive)

    def test_manual_plan_only_emits_selected_eligible_text_fields(self):
        self.web_archive([
            {"id_str": "123456701", "full_text": TEXT, "created_at": "2020-01-01", "screen_name": "PRIVATE_ACCOUNT", "unrelated": "PRIVATE_DATA"},
            {"id_str": "123456702", "full_text": TEXT, "retweeted_status_id_str": "9"},
            {"id_str": "123456703", "full_text": "短帖"},
            {"id_str": "123456704", "full_text": TEXT, "truncated": True},
            {"id_str": "123456705", "full_text": TEXT * 100, "in_reply_to_status_id_str": "8"},
        ])
        first = self.plan({"limit": 1})["result"]
        self.assertEqual(12, first["total_eligible"])
        self.assertEqual("123456701", first["posts"][0]["id"])
        self.assertFalse(first["done"])
        last = self.plan({"offset": first["next_offset"], "limit": 10, "max_chars": 100})["result"]
        self.assertEqual(first["session_id"], last["session_id"])
        self.assertTrue(last["done"])
        post = last["posts"][0]
        self.assertEqual("123456705", post["id"])
        self.assertEqual(100, len(post["text"]))
        self.assertTrue(post["text_truncated"])
        self.assertEqual(len(TEXT) * 100, post["original_chars"])
        self.assertEqual({"id", "text", "url", "created_at", "original_chars", "text_truncated"}, set(post))
        self.assertNotIn("PRIVATE", json.dumps(first))
        for invalid in ({"offset": -1}, {"offset": 11}, {"limit": 11}, {"limit": True}, {"max_chars": 5001}, {"max_chars": 0}):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.plan(invalid)["ok"])

    def test_apply_merges_batches_without_reanalysis_and_reports_partial_failure(self):
        base = self.web_archive([{"id_str": str(123456700 + i), "full_text": TEXT + str(i)} for i in range(3)])
        plan = self.plan()["result"]
        with patch.object(browser_api, "analyze", side_effect=AssertionError("must not rerun full archive")):
            matched = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": self.report(plan["posts"][0]["id"], "matched", [self.match()])})
        self.assertTrue(matched["ok"], matched)
        self.assertEqual({key: value for key, value in base["summary"].items() if key != "combined_evidence"},
                         {key: value for key, value in matched["result"]["summary"].items() if key != "combined_evidence"})
        self.assertIsNone(matched["result"]["summary"]["official_probability"])
        self.assertIn("policy_checks", matched["result"])
        self.assertFalse(any("未做全网查重" in text for text in matched["result"]["limitations"]))
        failed_report = self.report(plan["posts"][1]["id"], "failed", issues=[{"code": "provider_unavailable"}], checked_chars=0)
        failed = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": failed_report})["result"]
        coverage = failed["web_check"]["coverage"]
        self.assertEqual((2, 1, 1, 1, 8), tuple(coverage[key] for key in ("requested", "searched", "failed", "matched", "remaining")))
        self.assertEqual("unknown", coverage["web_coverage"])
        self.assertFalse(coverage["search_complete"])
        # Retrying a post updates its evidence, without double-counting the post.
        retry = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": self.report(plan["posts"][1]["id"])})["result"]
        self.assertEqual(2, retry["web_check"]["coverage"]["requested"])
        self.assertEqual(0, retry["web_check"]["coverage"]["failed"])

    def test_manual_ten_preserves_input_order_and_freezes_ids_without_changing_local_analysis(self):
        base = self.archive([{"id_str": str(123456700 + index), "full_text": TEXT + str(index)} for index in range(31)])
        self.assertEqual(31, base["summary"]["total"])
        self.assertTrue(base["summary"]["analyzed_all_archive_posts"])
        ids = [str(123456700 + index) for index in (30, 3, 26, 10, 13, 16, 20, 23, 6, 0)]
        first = self.plan({"links": self.links(ids), "limit": 3})["result"]
        self.assertEqual((31, 10, "manual_x_status_links"),
                         (first["total_eligible"], first["selected_total"], first["sample_method"]))
        selected = []
        plan = first
        while True:
            selected.extend(post["id"] for post in plan["posts"])
            if plan["done"]:
                break
            plan = self.plan({"offset": plan["next_offset"], "limit": 3})["result"]
        self.assertEqual(ids, selected)
        self.assertEqual(0, plan["remaining"])
        self.assertFalse(self.plan({"links": self.links(list(reversed(ids)))})["ok"])
        self.assertFalse(self.plan({"mode": "all"})["ok"])
        self.assertFalse(self.plan({"mode": "sample10"})["ok"])
        self.assertFalse(request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": self.report("123456701")})["ok"])
        self.assertFalse(self.plan({"offset": 11})["ok"])
        self.assertEqual(31, browser_api._retained_archive["base_result"]["summary"]["total"])
        self.assertTrue(self.plan({"links": [link.replace("x.com", "twitter.com") for link in self.links(ids)]})["ok"])

    def test_strict_ten_links_reject_invalid_duplicate_unavailable_or_ineligible_records_atomically(self):
        self.web_archive([
            {"id_str": "123456701", "full_text": TEXT},
            {"id_str": "123456702", "full_text": TEXT, "retweeted_status_id_str": "9"},
            {"id_str": "123456703", "full_text": "短帖"},
            {"id_str": "123456704", "full_text": TEXT, "truncated": True},
        ])
        links = self.links()
        invalid_links = [links[:9], links + ["https://x.com/example/status/9999"],
            ["", *links[1:]], [None, *links[1:]],
            ["https://x.com.attacker.example/example/status/123456701", *links[1:]],
            ["https://user:private@example.com/example/status/123456701", *links[1:]],
            ["http://127.0.0.1/example/status/123456701", *links[1:]],
            ["https://x.com/example/status/not-numeric", *links[1:]],
            ["https://x.com/example/status/123456701/extra", *links[1:]],
            ["https://x.com/example/status/999999999", *links[1:]],
            [links[0], links[0].replace("x.com", "twitter.com"), *links[2:]],
        ]
        invalid_links.extend([[f"https://x.com/example/status/{pid}", *links[1:]] for pid in ("123456702", "123456703", "123456704")])
        for candidate in invalid_links:
            with self.subTest(candidate=candidate):
                self.assertFalse(self.plan({"links": candidate})["ok"])
                self.assertFalse(browser_api._retained_archive["selection_locked"])
                self.assertEqual([], browser_api._retained_archive["selected"])
        self.assertFalse(self.plan({"limit": 11})["ok"])
        self.assertFalse(request("/api/webcheck/plan", {"mode": "manual10"})["ok"])
        self.assertFalse(request("/api/webcheck/plan")["ok"])
        for mode in ("all", "sample10"):
            self.assertFalse(request("/api/webcheck/plan", {"mode": mode, "links": links})["ok"])
        valid = self.plan({"links": [links[0] + "?s=20#media", *links[1:]]})
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(10, valid["result"]["selected_total"])
        self.assertNotIn("?s=20", json.dumps(valid))

    def test_archive_with_fewer_than_ten_eligible_posts_keeps_local_analysis_but_rejects_online(self):
        for records in ([{"id_str": str(123456700 + index), "full_text": TEXT} for index in range(4)],
                        [{"id_str": "123456789", "full_text": "短帖"}]):
            with self.subTest(size=len(records)):
                result = self.archive(records)
                self.assertEqual(len(records), result["summary"]["total"])
                rejected = self.plan()
                self.assertFalse(rejected["ok"])
                self.assertIn("不足 10", rejected["error"])
                self.assertFalse(browser_api._retained_archive["selection_locked"])
                self.assertEqual("not_selected", result["web_check"]["coverage"]["mode"])

    def test_result_readback_uses_current_manual_selection_without_queries_or_reanalysis(self):
        self.archive([{"id_str": str(123456700 + index), "full_text": TEXT + str(index)} for index in range(31)])
        self.plan({"limit": 3})
        with patch.object(browser_api, "analyze", side_effect=AssertionError("must not rerun analysis")):
            response = request("/api/webcheck/result")
        self.assertTrue(response["ok"], response)
        result = response["result"]
        self.assertEqual(("manual10", 10, 0), (result["web_check"]["coverage"]["mode"], result["web_check"]["coverage"]["selected_total"], result["web_check"]["coverage"]["searched"]))
        self.assertEqual("incomplete", result["summary"]["combined_evidence"]["status"])
        self.assertIn("手动指定的 10 条", result["summary"]["combined_evidence"]["conclusion"])
        self.assertEqual({}, browser_api._retained_archive["web_posts"])

    def test_completed_manual_ten_preserves_unselected_unknowns_and_offline_probability(self):
        base = self.archive([{"id_str": str(123456700 + index), "full_text": TEXT + str(index)} for index in range(31)])
        plan = self.plan()["result"]
        report = self.report(plan["posts"][0]["id"], "matched", [self.match()])
        for post in plan["posts"][1:]:
            report["posts"].extend(self.report(post["id"], sources_checked=1)["posts"])
        response = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})
        self.assertTrue(response["ok"], response)
        result = response["result"]
        coverage = result["web_check"]["coverage"]
        self.assertEqual((10, 0, 21), (coverage["selected_total"], coverage["remaining"], coverage["unselected_eligible"]))
        self.assertTrue(coverage["selection_complete"])
        self.assertTrue(coverage["selection_search_complete"])
        self.assertFalse(coverage["search_complete"])
        self.assertFalse(coverage["all_eligible_requested"])
        combined = result["summary"]["combined_evidence"]
        self.assertEqual("needs_review", combined["status"])
        self.assertEqual(1, combined["body_matched_posts"])
        self.assertEqual(21, combined["unknown_own_posts"])
        self.assertIn("不能推广", combined["conclusion"])
        self.assertFalse(combined["probability_recalculated"])
        self.assertEqual(base["summary"]["estimated_probability"], result["summary"]["estimated_probability"])
        self.assertEqual(base["summary"]["probability_range"], result["summary"]["probability_range"])
        original = next(item for item in result["policy_checks"]["requirements"] if item["id"] == "original_contribution")
        self.assertEqual((1, 31, 3.2), (original["signal_count"], original["denominator"], original["signal_percent"]))
        self.assertIsNone(result["summary"]["official_probability"])
        self.assertIn("综合证据结论", markdown_report({}, result))
        self.assertIn("公开来源证据合并", html_report({}, result))

    def test_abandoned_dispatched_batch_is_unknown_and_never_replanned(self):
        self.web_archive([{"id_str": str(123456700 + index), "full_text": TEXT + str(index)} for index in range(7)])
        plan = self.plan({"limit": 3})["result"]
        ids = [post["id"] for post in plan["posts"]]
        response = request("/api/webcheck/abandon", {"session_id": plan["session_id"], "ids": ids, "reason": "response_unknown"})
        self.assertTrue(response["ok"], response)
        coverage = response["result"]["web_check"]["coverage"]
        self.assertEqual((3, 7, 3, 0), (coverage["requested"], coverage["remaining"], coverage["execution_unknown_posts"], coverage["searched"]))
        self.assertEqual(sum(len(post["text"]) for post in plan["posts"]), coverage["unknown_execution_chars"])
        self.assertFalse(coverage["selection_search_complete"])
        for post in response["result"]["web_check"]["posts"]:
            self.assertEqual("unknown", post["execution_status"])
            self.assertEqual(0, post["checked_chars"])
            self.assertIsNone(post["max_similarity"])
            self.assertIn({"code": "batch_response_unknown"}, post["issues"])
        resumed = self.plan({"offset": 0, "limit": 3})["result"]
        self.assertEqual([str(123456700 + index) for index in (3, 4, 5)], [post["id"] for post in resumed["posts"]])
        self.assertEqual(6, resumed["next_offset"])
        self.assertIn("不表示没有检索消耗", " ".join(response["result"]["web_check"]["limitations"]))

    def test_abandon_requires_valid_session_ids_and_does_not_destroy_returned_evidence(self):
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}, {"id_str": "123456790", "full_text": TEXT}])
        plan = self.plan({"limit": 1})["result"]
        good = {"session_id": plan["session_id"], "ids": ["123456789"], "reason": "cancelled"}
        for bad in ({**good, "session_id": "stale"}, {**good, "ids": ["123456790"]}, {**good, "ids": ["123456789"] * 2}, {**good, "reason": "never_sent"}):
            self.assertFalse(request("/api/webcheck/abandon", bad)["ok"])
        self.assertEqual({}, browser_api._retained_archive["web_posts"])
        matched = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": self.report("123456789", "matched", [self.match()])})["result"]
        abandoned = request("/api/webcheck/abandon", good)["result"]
        self.assertEqual(matched["web_check"]["posts"], abandoned["web_check"]["posts"])
        self.assertEqual(0, abandoned["web_check"]["coverage"]["execution_unknown_posts"])

    def test_apply_keeps_local_truncation_and_snippet_unknowns(self):
        self.web_archive([{"id_str": "123456789", "full_text": TEXT * 100}])
        plan = self.plan({"max_chars": 100})["result"]
        match = self.match(source_kind="search_snippet", page_status="source_http_403", source_text_truncated=None, published_at=None, published_at_basis=None, temporal_relation="unknown")
        report = self.report("123456789", "partial", [match], original_chars=100, checked_chars=100, text_truncated=False, sources_checked=0, issues=[{"code": "source_http_403"}])
        result = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})
        self.assertTrue(result["ok"], result)
        post = result["result"]["web_check"]["posts"][0]
        self.assertTrue(post["text_truncated"])
        self.assertEqual(len(TEXT) * 100, post["original_chars"])
        self.assertIsNone(post["matches"][0]["source_text_truncated"])
        self.assertFalse(result["result"]["web_check"]["coverage"]["search_complete"])

    def test_unmatched_truncated_text_is_partial_and_match_totals_are_preserved(self):
        self.web_archive([{"id_str": "123456789", "full_text": TEXT * 100}])
        plan = self.plan({"max_chars": 100})["result"]
        report = self.report("123456789", checked_chars=100, original_chars=100, text_truncated=False)
        result = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})
        self.assertTrue(result["ok"], result)
        web = result["result"]["web_check"]
        self.assertEqual("partial", web["posts"][0]["status"])
        self.assertEqual(1, web["coverage"]["text_truncated_posts"])
        self.assertFalse(web["coverage"]["search_complete"])
        policy = next(row for row in result["result"]["policy_checks"]["requirements"] if row["id"] == "original_contribution")
        self.assertEqual(0, policy["assessed_count"])
        self.assertEqual(11, policy["unknown_count"])
        report = self.report("123456789", "matched", [self.match()] * 3, matches_total=5, checked_chars=100)
        result = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})
        self.assertTrue(result["ok"], result)
        self.assertEqual(5, result["result"]["web_check"]["posts"][0]["matches_total"])
        for invalid in (2, True, -1, 1001):
            report["posts"][0]["matches_total"] = invalid
            self.assertFalse(request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})["ok"])

    def test_unplanned_ids_stale_sessions_and_bad_evidence_do_not_mutate_state(self):
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}, {"id_str": "123456790", "full_text": TEXT + "新增"}])
        plan = self.plan({"limit": 1})["result"]
        payload = {"session_id": plan["session_id"], "report": self.report("123456790")}
        self.assertFalse(request("/api/webcheck/apply", payload)["ok"])
        self.assertFalse(request("/api/webcheck/apply", {**payload, "session_id": "old-session"})["ok"])
        bad_urls = ["file:///private", "https://user:password@example.com/source", "http://localhost/source", "http://127.0.0.1/source", "http://10.0.0.1/source", "http://[::1]/source", "http://169.254.169.254/source", "http://2130706433/source", "http://127.1/source", "http://0x7f000001/source", "https://example.com\\source", "https://example.com/\nsource"]
        for url in bad_urls:
            with self.subTest(url=url):
                report = self.report("123456789", "matched", [self.match(url=url)])
                self.assertFalse(request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})["ok"])
        self.assertEqual({}, browser_api._retained_archive["web_posts"])
        report = self.report("123456789", "matched", [self.match()] * 4)
        self.assertFalse(request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})["ok"])
        report = self.report("123456789", "matched", [self.match()])
        with patch.object(browser_api, "assess_policy", side_effect=RuntimeError("private error")):
            failed = request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})
        self.assertFalse(failed["ok"])
        self.assertNotIn("private error", failed["error"])
        self.assertEqual({}, browser_api._retained_archive["web_posts"])
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}])
        new = self.plan()["result"]
        self.assertNotEqual(plan["session_id"], new["session_id"])
        self.assertFalse(request("/api/webcheck/apply", {"session_id": plan["session_id"], "report": report})["ok"])

    def test_clear_and_new_archive_failures_remove_all_retained_content(self):
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}])
        self.assertTrue(request("/api/archive/clear")["result"]["cleared"])
        self.assertIsNone(browser_api._retained_archive)
        self.assertFalse(self.plan()["ok"])
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}])
        self.assertFalse(json.loads(browser_api.prepare_archive_json("not-json"))["ok"])
        self.assertIsNone(browser_api._retained_archive)
        self.web_archive([{"id_str": "123456789", "full_text": TEXT}])
        self.assertFalse(json.loads(browser_api.finish_archive_json("not-json"))["ok"])
        self.assertIsNone(browser_api._prepared_archive)
        self.assertIsNone(browser_api._retained_archive)


if __name__ == "__main__":
    unittest.main()
