"""Opt-in search API service. Keep provider credentials on this server only."""
from __future__ import annotations

import argparse
from collections import deque
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from webcheck import (MAX_CANDIDATES, MAX_CHECKED_TEXT, MAX_INPUT_TEXT, MAX_POSTS,
                      MAX_QUERIES, MIN_TEXT, SearchProvider, WebChecker, clean,
                      search_queries, validate_posts)

MAX_REQUEST = 700000
DEFAULT_QUERY_BUDGET = 100


class HourlyBudget:
    """Atomically reserve the entire batch before issuing paid search requests."""

    def __init__(self, limit, clock=time.monotonic):
        self.limit = limit
        self.clock = clock
        self.events = deque()
        self.lock = threading.Lock()

    def _expire(self, now):
        while self.events and self.events[0][0] <= now - 3600:
            self.events.popleft()

    def reserve(self, count):
        with self.lock:
            now = self.clock()
            self._expire(now)
            if sum(item[1] for item in self.events) + count > self.limit:
                return False
            if count:
                self.events.append((now, count))
            return True

    def remaining(self):
        with self.lock:
            self._expire(self.clock())
            return self.limit - sum(item[1] for item in self.events)


def valid_origin(value):
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Validate a supplied port, including its numeric bounds.
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
                    and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment and "*" not in value)
    except ValueError:
        return False


class WebCheckServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, checker, origins=None, hosts=None, access_token="", query_budget=DEFAULT_QUERY_BUDGET):
        self.checker = checker
        self.access_token = access_token
        self.allowed_origins = set(origins or [])
        self.allowed_hosts = set(hosts or [])
        self.budget = HourlyBudget(query_budget)
        self.slots = threading.BoundedSemaphore(2)
        try:
            local = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            local = address[0] == "localhost"
        if not local and (len(access_token) < 32 or not self.allowed_origins or not self.allowed_hosts):
            raise ValueError("公开绑定需要至少 32 字符的访问 token、允许的 Host 和 Origin。")
        if any(not valid_origin(origin) for origin in self.allowed_origins):
            raise ValueError("允许的 Origin 必须是明确的 http(s) 来源，不能含路径、查询或通配符。")
        if any("*" in host or "/" in host or not host for host in self.allowed_hosts):
            raise ValueError("允许的 Host 必须是明确主机名，可包含端口，不能使用通配符。")
        super().__init__(address, Handler)
        if local and not self.allowed_hosts:
            self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        if local and not self.allowed_origins:
            self.allowed_origins = {f"http://127.0.0.1:{self.server_port}", f"http://localhost:{self.server_port}", "https://potervlag-cyber.github.io"}

    def status(self):
        provider = self.checker.provider
        return {"ok": True, "schema_version": 1, "ready": provider.ready, "provider": provider.name,
                "requires_access_token": bool(self.access_token), "limits": {"max_posts": MAX_POSTS,
                "min_text": MIN_TEXT, "max_input_chars": MAX_INPUT_TEXT, "max_checked_chars": MAX_CHECKED_TEXT,
                "max_queries_per_post": MAX_QUERIES, "max_candidates_per_post": MAX_CANDIDATES,
                "hourly_query_budget": self.budget.limit, "hourly_queries_remaining": self.budget.remaining(), "max_concurrent_batches": 2}}


class Handler(BaseHTTPRequestHandler):
    server_version = "OriginalityWebCheck/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, _format, *_args):
        # HTTP queries, auth headers, bodies, page content, and exceptions are never logged.
        return

    def _allowed(self):
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in self.server.allowed_hosts and (not origin or origin in self.server.allowed_origins)

    def _authorized(self):
        if not self.server.access_token:
            return True
        expected = "Bearer " + self.server.access_token
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        origin = self.headers.get("Origin")
        if origin in self.server.allowed_origins and self.headers.get("Host") in self.server.allowed_hosts:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (OSError, TimeoutError):
            pass

    def do_OPTIONS(self):
        if not self._allowed():
            return self.send_json({"error": "请求来源不在服务端允许列表中。"}, 403)
        if urlsplit(self.path).path not in {"/api/webcheck", "/api/webcheck/status"}:
            return self.send_json({"error": "接口不存在。"}, 404)
        return self.send_json({"ok": True})

    def do_GET(self):
        if not self._allowed():
            return self.send_json({"error": "请求来源或 Host 不在服务端允许列表中。"}, 403)
        if urlsplit(self.path).path != "/api/webcheck/status":
            return self.send_json({"error": "接口不存在。"}, 404)
        # Status deliberately excludes credentials and submitted content.
        return self.send_json(self.server.status())

    def do_POST(self):
        if not self._allowed():
            return self.send_json({"error": "请求来源或 Host 不在服务端允许列表中。"}, 403)
        if not self._authorized():
            return self.send_json({"error": "服务访问 token 无效。", "code": "unauthorized"}, 401)
        if urlsplit(self.path).path != "/api/webcheck":
            return self.send_json({"error": "接口不存在。"}, 404)
        if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            return self.send_json({"error": "请使用 JSON 请求。"}, 415)
        if self.headers.get("Transfer-Encoding"):
            return self.send_json({"error": "不支持分块请求。"}, 400)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "请求长度无效。"}, 400)
        if not 0 < length <= MAX_REQUEST:
            return self.send_json({"error": "请求过大或为空。"}, 413)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求正文不完整。")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) - {"posts", "consent"}:
                raise ValueError("请求仅接受 posts 与 consent 字段。")
            if payload.get("consent") is not True:
                raise ValueError("请先明确同意将待检索正文发送给搜索服务。")
            posts = validate_posts(payload.get("posts"))
        except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
            # Parser exceptions contain submitted strings; expose only our known messages.
            message = str(exc) if type(exc) is ValueError and not isinstance(exc, json.JSONDecodeError) else "请求 JSON 无效。"
            return self.send_json({"error": message}, 400)
        except (OSError, TimeoutError):
            return self.send_json({"error": "接收请求超时。"}, 408)
        if not self.server.checker.provider.ready:
            return self.send_json({"error": "服务端尚未配置搜索 API。", "code": "provider_not_configured"}, 503)
        if not self.server.slots.acquire(blocking=False):
            return self.send_json({"error": "服务正在处理其他批次，请稍后重试。", "code": "busy"}, 429)
        try:
            queries = sum(len(search_queries(p["text"][:MAX_CHECKED_TEXT])) for p in posts if len(clean(p["text"][:MAX_CHECKED_TEXT])) >= MIN_TEXT)
            if not self.server.budget.reserve(queries):
                return self.send_json({"error": "本小时检索预算已用完，请稍后重试。", "code": "budget_exhausted"}, 429)
            report = self.server.checker.check(posts)
            return self.send_json(report)
        except Exception:
            # Never expose upstream bodies, tokens, exception messages, or stack traces.
            return self.send_json({"error": "联网检查内部失败，本批次未获得完整结果。", "code": "internal_error"}, 500)
        finally:
            self.server.slots.release()


def make_server(host="127.0.0.1", port=8787, env=None):
    settings = os.environ if env is None else env
    provider_name = settings.get("WEBCHECK_PROVIDER", "tavily").lower().strip()
    if provider_name not in {"tavily", "brave"}:
        raise ValueError("WEBCHECK_PROVIDER 必须是 tavily 或 brave。")
    key = settings.get("TAVILY_API_KEY" if provider_name == "tavily" else "BRAVE_SEARCH_API_KEY", "")
    provider = SearchProvider(provider_name, key)
    origins = [origin.strip().rstrip("/") for origin in settings.get("WEBCHECK_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
    hosts = [value.strip() for value in settings.get("WEBCHECK_ALLOWED_HOSTS", "").split(",") if value.strip()]
    budget = int(settings.get("WEBCHECK_HOURLY_QUERY_BUDGET", str(DEFAULT_QUERY_BUDGET)))
    if not 1 <= budget <= 10000:
        raise ValueError("小时预算必须是 1–10,000 次查询。")
    return WebCheckServer((host, port), WebChecker(provider), origins, hosts, settings.get("WEBCHECK_ACCESS_TOKEN", ""), budget)


def main():
    parser = argparse.ArgumentParser(description="原作：公开网页联网文字查重服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--generate-access-token", action="store_true", help="只生成新的服务访问 token，不启动服务")
    args = parser.parse_args()
    if args.generate_access_token:
        print(secrets.token_urlsafe(32))
        return
    try:
        server = make_server(args.host, args.port)
    except (ValueError, OSError):
        parser.exit(2, "服务配置无效或端口不可用。检查环境变量、明确 Host/Origin 及远程访问 token。\n")
    print(f"公开网页查重服务已启动，端口 {server.server_port}。搜索配置：{'就绪' if server.checker.provider.ready else '待配置'}。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
