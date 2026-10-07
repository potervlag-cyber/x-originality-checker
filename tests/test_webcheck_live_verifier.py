"""The verifier must reject configuration-only and snippet-only success claims."""
import json
import unittest
from unittest.mock import Mock

from deployment.check_webcheck_live import SAMPLE_ID, assess_report, check, service_url
from webcheck import HttpResult, WebCheckError


class LiveVerifierTests(unittest.TestCase):
    def report(self):
        return {"schema_version": 1, "provider": "tavily", "posts": [{"id": SAMPLE_ID,
            "status": "matched", "successful_queries": 1, "sources_checked": 1, "issues": [],
            "matches": [{"source_kind": "page_body", "page_status": "fetched", "score": 0.9,
                "matched_chars": 80, "source_excerpt": "Public quotation body evidence " * 3,
                "url": "https://docs.python.org/3/tutorial/"}]}]}

    def test_only_fetched_body_search_evidence_passes(self):
        self.assertEqual(1, len(assess_report(self.report())["source_evidence"]))
        for changes in ({"status": "failed"}, {"successful_queries": 0}, {"sources_checked": 0}, {"id": "other"}):
            report = self.report()
            report["posts"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                assess_report(report)
        for changes in ({"source_kind": "search_snippet"}, {"page_status": "source_http_failed"}, {"score": 0.1}, {"matched_chars": 2}, {"url": "http://127.0.0.1/private"}):
            report = self.report()
            report["posts"][0]["matches"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises((ValueError, WebCheckError)):
                assess_report(report)

    def test_unconfigured_service_does_not_issue_search(self):
        transport = Mock()
        transport.request.return_value = HttpResult("https://check.example/api/webcheck/status", 200, "application/json", b'{"ready":false}')
        with self.assertRaisesRegex(ValueError, "not_configured"):
            check("https://check.example", "private-service-token", transport)
        self.assertEqual(1, transport.request.call_count)

    def test_request_is_bounded_and_receipt_never_contains_token(self):
        transport = Mock()
        transport.request.side_effect = [HttpResult("https://check.example/api/webcheck/status", 200, "application/json", b'{"ready":true}'),
            HttpResult("https://check.example/api/webcheck", 200, "application/json", json.dumps(self.report()).encode())]
        result = check("https://check.example", "private-service-token", transport)
        self.assertEqual("PASS", result["status"])
        self.assertNotIn("private-service-token", json.dumps(result))
        request = transport.request.call_args
        payload = json.loads(request.kwargs["body"])
        self.assertIs(True, payload["consent"])
        self.assertEqual({"id", "text", "url", "created_at"}, set(payload["posts"][0]))
        self.assertEqual(0, request.kwargs["redirects"])

    def test_target_validation_precedes_secret_use(self):
        for endpoint in ("http://check.example", "https://user:password@check.example", "https://check.example/?secret=x", "https://check.example/#fragment", "https://127.0.0.1"):
            transport = Mock()
            with self.subTest(endpoint=endpoint), self.assertRaises((ValueError, WebCheckError)):
                check(endpoint, "private-service-token", transport)
            transport.request.assert_not_called()
        self.assertEqual("https://check.example/prefix", service_url("https://check.example/prefix/"))


if __name__ == "__main__":
    unittest.main()
