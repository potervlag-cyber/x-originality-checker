"""Local-only GUI host for X originality evidence screening. No dependencies."""
from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_REQUEST = 45 * 1024 * 1024
MAX_FILE = 30 * 1024 * 1024
VERSION = "0.1.0"


from reports import markdown_report, html_report


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server_version = "OriginalityLocal/" + VERSION

    def log_message(self, _format, *_args):
        # Do not log imported content, request bodies, or user identifiers.
        return

    def response(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self' https: http://127.0.0.1:* http://localhost:*; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        self.response(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def allowed_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.allowed_host():
            return self.send_json({"error": "只允许本机访问。"}, 403)
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.send_json({"ok": True, "app": "x-originality-checker", "version": VERSION, "token": self.server.token})
        if path == "/api/sample":
            return self.response((ROOT / "sample-data.json").read_bytes(), "application/json; charset=utf-8")
        if path == "/api/template":
            source = ROOT.parent / "X原创检测提交表模板.md"
            if not source.is_file():
                source = ROOT / "materials" / "template.md"
            return self.response(source.read_bytes(), "text/markdown; charset=utf-8")
        if path == "/api/guide":
            source = ROOT.parent / "X原创检测材料提交说明.md"
            if not source.is_file():
                source = ROOT / "materials" / "guide.md"
            return self.send_json({"text": source.read_text(encoding="utf-8-sig")})
        browser_routes = {"/" + name: ROOT / "browser" / name for name in ("runtime.js", "python-worker.js", "archive.js", "browser_api.py", "webcheck-client.js")}
        browser_routes.update({"/" + name: ROOT / name for name in ("archive_adapter.py", "policy_checks.py", "engine.py", "importers.py", "reports.py")})
        browser_routes["/webcheck-config.json"] = ROOT / "deployment/webcheck-config.json"
        runtime_names = ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json", "LICENSE.pyodide", "LICENSE.cpython")
        browser_routes.update({"/vendor/pyodide/" + name: ROOT / "vendor" / "pyodide" / name for name in runtime_names})
        if path in browser_routes:
            source = browser_routes[path]
            if not source.is_file():
                return self.send_json({"error": "浏览器运行文件缺失，请先运行 build_site.py --output _site --download-runtime。"}, 500)
            content_type = {".js": "text/javascript; charset=utf-8", ".wasm": "application/wasm", ".json": "application/json", ".zip": "application/zip"}.get(source.suffix, "text/plain; charset=utf-8")
            return self.response(source.read_bytes(), content_type)
        routes = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}
        if path not in routes:
            return self.send_json({"error": "页面不存在。"}, 404)
        file = STATIC / routes[path]
        try:
            self.response(file.read_bytes(), {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}[file.suffix])
        except FileNotFoundError:
            self.send_json({"error": "应用文件缺失，请重新检查安装目录。"}, 500)

    def do_POST(self):
        # Drain a bounded body before sending any early rejection. Closing a Windows
        # socket with unread incoming bytes can reset it before the client reads 403.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send_json({"error": "请求长度无效。"}, 400)
        if length <= 0 or length > MAX_REQUEST:
            return self.send_json({"error": "材料过大，单次请求最多 45 MB。请分批导入。"}, 413)
        self.connection.settimeout(15)
        try:
            request_body = self.rfile.read(length)
        except TimeoutError:
            return self.send_json({"error": "接收材料超时，请重新导入。"}, 408)
        if len(request_body) != length:
            return self.send_json({"error": "材料接收不完整，请重新导入。"}, 400)
        allowed_origins = {self.server.origin, f"http://localhost:{self.server.server_port}"}
        if not self.allowed_host() or self.headers.get("Origin") not in allowed_origins or not secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.token):
            return self.send_json({"error": "请求来源不匹配，请从本机应用页面操作。"}, 403)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_json({"error": "请使用 JSON 请求。"}, 415)
        try:
            data = json.loads(request_body)
            if not isinstance(data, dict):
                raise ValueError("请求需要包含对象。")
            path = urlsplit(self.path).path
            if path == "/api/shutdown":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/import":
                from importers import import_data
                filename = str(data.get("filename", ""))
                encoded = data.get("content_base64", "")
                if not isinstance(encoded, str) or len(encoded) > MAX_FILE * 4 / 3 + 8:
                    raise ValueError("单个材料文件最多 30 MB。")
                content = base64.b64decode(encoded, validate=True)
                if len(content) > MAX_FILE:
                    raise ValueError("单个材料文件最多 30 MB。")
                return self.send_json(import_data(content, filename))
            if path in {"/api/analyze", "/api/report"}:
                from engine import analyze
                from importers import normalize_project
                project = normalize_project(data.get("project", {}))
                result = analyze(project)
                if path == "/api/analyze":
                    return self.send_json({"project": project, "analysis": result})
                report_format = data.get("format", "markdown")
                report = html_report(project, result) if report_format == "html" else markdown_report(project, result)
                return self.send_json({"content": report, "format": report_format})
            self.send_json({"error": "接口不存在。"}, 404)
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError, OverflowError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception:
            # Avoid exposing paths or submitted private content in exception messages.
            self.send_json({"error": "处理材料时遇到内部错误。请检查材料格式，或查看本地测试说明。"}, 500)


def main():
    parser = argparse.ArgumentParser(description="X 原创检测，本机 GUI")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = LocalServer(("127.0.0.1", args.port))
    except OSError:
        if args.port == 0:
            raise
        server = LocalServer(("127.0.0.1", 0))
    print(f"X_ORIGINALITY_URL={server.origin}", flush=True)
    print("只在本机运行。关闭进程即可停止；项目需手动保存 JSON。", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(server.origin)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
