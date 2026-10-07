"""Fixture tests are not evidence of coverage or real public-search success."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from webcheck import (HttpResult, SafeHTTPTransport, SearchCandidate, SearchProvider,
                      WebChecker, WebCheckError, compare_text, page_text, public_url,
                      resolve_public, same_post, temporal_relation, validate_posts)
from webcheck_server import HourlyBudget, WebCheckServer, make_server

ORIGINAL = "我连续记录了两周的写作过程，先整理问题，再补充来源，最后检查观点是否由材料支持。最耗时的步骤不是写初稿，而是找到能复现结论的记录。"
ENGLISH = "The experiment measured loading latency and failure rates on the same device using three publication methods over two weeks. Every observation included the precise conditions needed to reproduce the result."


class StaticProvider:
    name = "fixture_only"
    ready = True

    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.error:
            raise WebCheckError(self.error)
        return self.candidates


class StaticTransport:
    def __init__(self, content=ORIGINAL, error=None, url="https://source.example/article"):
        self.content = content
        self.error = error
        self.url = url
        self.calls = []

    def request(self, url, *args, **kwargs):
        self.calls.append(url)
        if self.error:
            raise WebCheckError(self.error)
        return HttpResult(self.url, 200, "text/html; charset=utf-8", f'<html><title>Fixture source</title><meta property="article:published_time" content="2026-01-01T00:00:00Z"><article>{self.content}</article></html>'.encode())


class PublicTransportTests(unittest.TestCase):
    def test_rejects_credentials_non_http_and_nonstandard_ports(self):
        for url in ("file:///secret", "https://user:key@example.com/", "http://example.com:8080/", "https://example.com:80/", "https://example.com\\@127.0.0.1/", "https://exa%6dple.com/"):
            with self.subTest(url=url), self.assertRaises(WebCheckError):
                public_url(url)

    def test_resolver_rejects_all_private_and_mixed_answers(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "0.0.0.0", "2002:7f00:1::", "64:ff9b::a00:1"):
            answers = [(2, 1, 6, "", ("93.184.216.34", 80)), (2, 1, 6, "", (address, 80))]
            with self.subTest(address=address), patch("webcheck.socket.getaddrinfo", return_value=answers), self.assertRaises(WebCheckError):
                resolve_public("example.com", 80)

    def test_checked_numeric_address_is_pinned_for_connect(self):
        connection = Mock()
        response = Mock(status=200)
        response.getheader.side_effect = lambda key, default=None: {"Content-Type": "text/plain", "Content-Length": "2"}.get(key, default)
        response.read1.side_effect = [b"ok", b""]
        connection.getresponse.return_value = response
        with patch("webcheck.resolve_public", return_value=["93.184.216.34"]), patch("webcheck._PinnedHTTPConnection", return_value=connection) as constructor:
            result = SafeHTTPTransport().request("http://example.com/article")
        self.assertEqual(b"ok", result.body)
        self.assertEqual(("example.com", 80, "93.184.216.34"), constructor.call_args.args[:3])
        self.assertEqual("/article", connection.request.call_args.args[1])

    def test_each_redirect_revalidates_dns_and_cannot_visit_private_network(self):
        response = Mock(status=302)
        response.getheader.return_value = "http://internal.example/metadata"
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("webcheck.resolve_public", side_effect=[["93.184.216.34"], WebCheckError("source_blocked")]) as resolver, patch("webcheck._PinnedHTTPConnection", return_value=connection) as constructor:
            with self.assertRaisesRegex(WebCheckError, "source_blocked"):
                SafeHTTPTransport().request("http://public.example/start")
        self.assertEqual(2, resolver.call_count)
        self.assertEqual(1, constructor.call_count)

    def test_provider_headers_cannot_leak_on_redirect(self):
        response = Mock(status=302)
        response.getheader.return_value = "https://attacker.example/"
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("webcheck.resolve_public", return_value=["93.184.216.34"]), patch("webcheck._PinnedHTTPSConnection", return_value=connection) as constructor:
            with self.assertRaisesRegex(WebCheckError, "source_redirect_limit"):
                SafeHTTPTransport().request("https://api.search.brave.com/", headers={"X-Subscription-Token": "fixture-key"}, redirects=0)
        self.assertEqual(1, constructor.call_count)

    def test_read_size_and_encoding_are_bounded(self):
        for headers, chunks, code in (({"Content-Length": "101"}, [], "source_too_large"), ({}, [b"x" * 101], "source_too_large"), ({"Content-Encoding": "gzip"}, [], "source_unsupported_encoding"), ({"Content-Length": "3"}, [b"ok", b""], "source_incomplete")):
            response = Mock(status=200)
            response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
            response.read1.side_effect = chunks
            connection = Mock()
            connection.getresponse.return_value = response
            with self.subTest(code=code), patch("webcheck.resolve_public", return_value=["93.184.216.34"]), patch("webcheck._PinnedHTTPConnection", return_value=connection):
                with self.assertRaisesRegex(WebCheckError, code):
                    SafeHTTPTransport().request("http://example.com/", max_bytes=100)


class ComparisonTests(unittest.TestCase):
    def post(self, text=ORIGINAL):
        return {"id": "12345", "text": text, "url": "https://x.com/me/status/12345", "created_at": "2026-02-01T00:00:00Z"}

    def test_exact_text_inside_long_public_article(self):
        match = compare_text(ORIGINAL, "前面是完全不同的信息。" * 700 + ORIGINAL + "后续信息。" * 700)
        self.assertEqual(1, match["score"])
        self.assertEqual(ORIGINAL.split("。")[0], match["post_excerpt"].split("。")[0])

    def test_near_english_text_matches_without_character_autojunk(self):
        source = ENGLISH.replace("loading latency", "response latency").replace("two weeks", "three weeks")
        match = compare_text(ENGLISH, "Other information. " + source)
        self.assertIsNotNone(match)
        self.assertGreater(match["score"], 0.7)

    def test_unrelated_text_is_not_reported(self):
        self.assertIsNone(compare_text(ORIGINAL, "该项目测试的是海洋环境下的传感器，在不同深度收集温度和盐度数据，并提供实时定位与设备状态。"))

    def test_unicode_expansion_counts_original_characters(self):
        text = "ﬃ" * 100 + ORIGINAL
        match = compare_text(text, "ffi" * 100 + ORIGINAL)
        self.assertLessEqual(match["matched_chars"], len(text))
        self.assertEqual(1, match["score"])

    def test_date_order_requires_timezone_and_is_only_claimed_metadata(self):
        self.assertEqual("unknown", temporal_relation("2026-01-01", "2026-02-01T00:00:00Z"))
        self.assertEqual("unknown", temporal_relation("not a date", "2026-02-01T00:00:00Z"))
        self.assertEqual("earlier", temporal_relation("2026-01-01T00:00:00Z", "2026-02-01T00:00:00+08:00"))
        self.assertEqual("later", temporal_relation("2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z"))

    def test_html_excludes_scripts_navigation_and_extracts_publication(self):
        response = HttpResult("https://source.example/", 200, "text/html", ('<nav>navigation</nav><script>secret script</script><script type="application/ld+json">{"@type":"Article","datePublished":"2026-01-01T00:00:00Z"}</script><title>Example</title><article>' + ORIGINAL + '</article>').encode())
        parsed = page_text(response)
        self.assertEqual(ORIGINAL, parsed["text"])
        self.assertEqual("Example", parsed["title"])
        self.assertEqual("2026-01-01T00:00:00Z", parsed["published_at"])

    def test_multiple_json_ld_blocks_extract_article_date(self):
        html = '<script type="application/ld+json">{"@type":"Organization"}</script><script type="application/ld+json">{"@type":"Article","datePublished":"2026-01-01T00:00:00Z"}</script><p>' + ORIGINAL + '</p>'
        parsed = page_text(HttpResult("https://source.example/", 200, "text/html", html.encode()))
        self.assertEqual("2026-01-01T00:00:00Z", parsed["published_at"])

    def test_same_x_post_across_domains_excluded_from_external_matches(self):
        provider = StaticProvider([SearchCandidate("https://twitter.com/other/status/12345", "Own post", ORIGINAL)])
        transport = StaticTransport()
        result = WebChecker(provider, transport).check([self.post()])
        self.assertEqual([], result["posts"][0]["matches"])
        self.assertEqual("same_post", result["posts"][0]["same_post"][0]["reason"])
        self.assertEqual([], transport.calls)
        self.assertFalse(same_post("https://attacker.example/me/status/12345", self.post()))

    def test_redirect_to_own_x_post_excluded(self):
        provider = StaticProvider([SearchCandidate("https://source.example/article", "Source", ORIGINAL)])
        result = WebChecker(provider, StaticTransport(url="https://x.com/any/status/12345")).check([self.post()])
        self.assertEqual([], result["posts"][0]["matches"])
        self.assertEqual(1, len(result["posts"][0]["same_post"]))

    def test_search_failure_is_unknown_and_not_zero_or_original(self):
        result = WebChecker(StaticProvider(error="provider_request_failed"), StaticTransport()).check([self.post()])
        self.assertEqual("failed", result["posts"][0]["status"])
        self.assertIsNone(result["posts"][0]["max_similarity"])
        self.assertEqual("unknown", result["coverage"]["web_coverage"])
        self.assertEqual(0, result["coverage"]["searched"])

    def test_successful_empty_search_still_does_not_prove_originality(self):
        result = WebChecker(StaticProvider(), StaticTransport()).check([self.post()])
        self.assertEqual("no_match", result["posts"][0]["status"])
        self.assertIsNone(result["posts"][0]["max_similarity"])
        self.assertEqual(0, result["coverage"]["compared"])
        self.assertIn("未找到重复不证明原创", result["limitations"][0])

    def test_snippet_and_fetched_page_evidence_remain_distinct(self):
        provider = StaticProvider([SearchCandidate("https://source.example/article", "Source", ORIGINAL, "2026-01-01T00:00:00Z")])
        page = WebChecker(provider, StaticTransport()).check([self.post()])["posts"][0]
        snippet = WebChecker(provider, StaticTransport(error="source_http_failed")).check([self.post()])["posts"][0]
        self.assertEqual("page_body", page["matches"][0]["source_kind"])
        self.assertEqual("search_snippet", snippet["matches"][0]["source_kind"])
        self.assertEqual("partial", snippet["status"])
        self.assertEqual(0, snippet["sources_checked"])
        self.assertEqual("page_metadata", page["matches"][0]["published_at_basis"])
        self.assertEqual("search_result", snippet["matches"][0]["published_at_basis"])

    def test_blocked_links_never_return_snippet_evidence(self):
        provider = StaticProvider([SearchCandidate("https://internal.example/article", "Source", ORIGINAL)])
        result = WebChecker(provider, StaticTransport(error="source_blocked")).check([self.post()])["posts"][0]
        self.assertEqual([], result["matches"])
        self.assertEqual("partial", result["status"])

    def test_empty_or_javascript_only_page_is_not_compared_body(self):
        provider = StaticProvider([SearchCandidate("https://source.example/article", "Source", ORIGINAL)])
        result = WebChecker(provider, StaticTransport(content="<script>Only client rendering</script>")).check([self.post()])["posts"][0]
        self.assertEqual(0, result["sources_checked"])
        self.assertEqual("search_snippet", result["matches"][0]["source_kind"])
        self.assertEqual("source_no_comparable_text", result["matches"][0]["page_status"])

    def test_truncated_source_remains_partial(self):
        provider = StaticProvider([SearchCandidate("https://source.example/article", "Source", ORIGINAL)])
        result = WebChecker(provider, StaticTransport(content=ORIGINAL + "z" * 100001)).check([self.post()])["posts"][0]
        self.assertEqual("partial", result["status"])
        self.assertTrue(result["matches"][0]["source_text_truncated"])

    def test_short_and_truncated_posts_are_explicitly_counted(self):
        short = self.post("hello")
        long = {**self.post(ORIGINAL + "其余尚未检查的内容。" * 500), "id": "long"}
        result = WebChecker(StaticProvider(), StaticTransport()).check([short, long])
        self.assertEqual("skipped", result["posts"][0]["status"])
        self.assertTrue(result["posts"][1]["text_truncated"])
        self.assertEqual(5000, result["posts"][1]["checked_chars"])
        self.assertEqual(len(long["text"]), result["posts"][1]["original_chars"])
        self.assertEqual("partial", result["posts"][1]["status"])

    def test_exact_request_whitelist_and_duplicate_id_validation(self):
        for posts in ([{**self.post(), "private_messages": []}], [self.post(), self.post()], [{**self.post(), "text": "x" * 50001}], []):
            with self.assertRaises(ValueError):
                validate_posts(posts)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def _response(self, body, media="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(("tavily", data["query"]))
        self._response(json.dumps({"results": [{"url": "https://source.example/article", "title": "HTTP fixture", "content": ORIGINAL}]}).encode())

    def do_GET(self):
        if self.path.startswith("/res/v1/web/search?"):
            self.server.calls.append(("brave", self.path))
            return self._response(json.dumps({"web": {"results": [{"url": "https://source.example/article", "title": "HTTP fixture", "description": ORIGINAL}]}}).encode())
        self.server.calls.append(("page", self.path))
        self._response(('<html><title>Fixture article</title><meta property="article:published_time" content="2026-01-01T00:00:00Z"><article>' + ORIGINAL + '</article></html>').encode(), "text/html; charset=utf-8")


class FixtureHTTPTransport:
    """Test-only remapping; production transport cannot access loopback sources."""

    def __init__(self, server):
        self.port = server.server_port

    def request(self, url, method="GET", headers=None, body=None, **kwargs):
        parsed = urlsplit(url)
        if parsed.hostname not in {"api.tavily.com", "api.search.brave.com", "source.example"}:
            raise AssertionError("Fixture transport received an unexpected host")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body=body, headers=headers or {})
            response = connection.getresponse()
            return HttpResult(url, response.status, response.getheader("Content-Type"), response.read())
        finally:
            connection.close()


class RealLocalHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.fixture.calls = []
        cls.fixture_thread = threading.Thread(target=cls.fixture.serve_forever, daemon=True)
        cls.fixture_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.shutdown()
        cls.fixture.server_close()
        cls.fixture_thread.join()

    def start_service(self, name="tavily", token="", budget=100):
        transport = FixtureHTTPTransport(self.fixture)
        provider = SearchProvider(name, "not-a-real-key", transport)
        server = WebCheckServer(("127.0.0.1", 0), WebChecker(provider, transport), origins=["https://potervlag-cyber.github.io"], access_token=token, query_budget=budget)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, server, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request_headers = {"Origin": "https://potervlag-cyber.github.io", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=json.dumps(payload).encode() if payload is not None else None, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def payload(self):
        return {"consent": True, "posts": [{"id": "own", "text": ORIGINAL, "url": "https://x.com/me/status/12345", "created_at": "2026-02-01T00:00:00Z"}]}

    def test_real_http_search_fetch_parse_compare_report_for_both_providers(self):
        for provider in ("tavily", "brave"):
            with self.subTest(provider=provider):
                server = self.start_service(provider)
                status, headers, result = self.call(server, "POST", "/api/webcheck", self.payload())
                self.assertEqual(200, status)
                self.assertEqual("https://potervlag-cyber.github.io", headers["Access-Control-Allow-Origin"])
                self.assertEqual("matched", result["posts"][0]["status"])
                match = result["posts"][0]["matches"][0]
                self.assertEqual(1, match["score"])
                self.assertEqual("page_body", match["source_kind"])
                self.assertEqual("earlier", match["temporal_relation"])
                self.assertEqual("unknown", result["coverage"]["web_coverage"])
                self.assertNotIn("not-a-real-key", json.dumps(result))
                self.assertNotIn("text", result["posts"][0])
        self.assertTrue(any(call[0] == "tavily" for call in self.fixture.calls))
        self.assertTrue(any(call[0] == "brave" for call in self.fixture.calls))
        self.assertTrue(any(call[0] == "page" for call in self.fixture.calls))

    def test_status_is_secret_free_and_declares_bounds(self):
        server = self.start_service(token="a" * 40)
        status, _, result = self.call(server, "GET", "/api/webcheck/status")
        self.assertEqual(200, status)
        self.assertTrue(result["requires_access_token"])
        self.assertEqual(10, result["limits"]["max_posts"])
        self.assertNotIn("not-a-real-key", json.dumps(result))
        self.assertNotIn("a" * 40, json.dumps(result))

    def test_consent_token_origin_host_and_field_whitelist_are_enforced(self):
        server = self.start_service(token="a" * 40)
        token_header = {"Authorization": "Bearer " + "a" * 40}
        cases = [(self.payload(), {}, 401), ({**self.payload(), "consent": False}, token_header, 400),
                 (self.payload(), {**token_header, "Origin": "https://attacker.example"}, 403),
                 (self.payload(), {**token_header, "Host": "attacker.example"}, 403),
                 ({**self.payload(), "project": {"messages": "private"}}, token_header, 400)]
        for payload, headers, expected in cases:
            with self.subTest(expected=expected, headers=headers):
                self.assertEqual(expected, self.call(server, "POST", "/api/webcheck", payload, headers)[0])
        self.assertEqual(200, self.call(server, "POST", "/api/webcheck", self.payload(), token_header)[0])

    def test_global_query_budget_returns_explicit_failure(self):
        server = self.start_service(budget=1)
        self.assertEqual(200, self.call(server, "POST", "/api/webcheck", self.payload())[0])
        status, _, result = self.call(server, "POST", "/api/webcheck", self.payload())
        self.assertEqual(429, status)
        self.assertEqual("budget_exhausted", result["code"])

    def test_unconfigured_provider_is_not_a_successful_empty_search(self):
        server = self.start_service()
        server.checker.provider.key = ""
        status, _, result = self.call(server, "POST", "/api/webcheck", self.payload())
        self.assertEqual(503, status)
        self.assertEqual("provider_not_configured", result["code"])


class ConfigurationTests(unittest.TestCase):
    def test_remote_binding_requires_token_host_and_origin_before_binding(self):
        checker = WebChecker(StaticProvider())
        for params in ({}, {"access_token": "x" * 40}, {"access_token": "x" * 40, "origins": ["https://example.com"]}):
            with self.assertRaises(ValueError):
                WebCheckServer(("0.0.0.0", 0), checker, **params)

    def test_wildcard_or_path_origins_rejected(self):
        for origin in ("https://*.example.com", "https://example.com/app", "null", "https://user:key@example.com"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                WebCheckServer(("127.0.0.1", 0), WebChecker(StaticProvider()), origins=[origin])

    def test_env_never_supports_arbitrary_search_endpoint(self):
        server = make_server(port=0, env={"WEBCHECK_PROVIDER": "brave", "BRAVE_SEARCH_API_KEY": "fixture", "WEBCHECK_ENDPOINT": "http://127.0.0.1/"})
        self.addCleanup(server.server_close)
        self.assertIsNone(server.checker.provider.endpoint)
        self.assertEqual("brave", server.checker.provider.name)

    def test_hourly_budget_is_atomic_and_expires(self):
        now = [1.0]
        budget = HourlyBudget(3, lambda: now[0])
        self.assertTrue(budget.reserve(2))
        self.assertFalse(budget.reserve(2))
        self.assertEqual(1, budget.remaining())
        now[0] = 3602
        self.assertEqual(3, budget.remaining())
        self.assertTrue(budget.reserve(3))


if __name__ == "__main__":
    unittest.main()
