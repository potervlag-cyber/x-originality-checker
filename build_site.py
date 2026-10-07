"""Build the allowlisted GitHub Pages site, without uploading user materials."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
PYODIDE_VERSION = "0.27.7"
RUNTIME_BASE = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
RUNTIME_URLS = {
    name: RUNTIME_BASE + name
    for name in (
        "pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm",
        "python_stdlib.zip", "pyodide-lock.json",
    )
}
RUNTIME_URLS.update({
    "LICENSE.pyodide": "https://raw.githubusercontent.com/pyodide/pyodide/0.27.7/LICENSE",
    "LICENSE.cpython": "https://raw.githubusercontent.com/python/cpython/v3.12.7/LICENSE",
})
MAX_RUNTIME_FILE = 20 * 1024 * 1024
SITE_SOURCES = {
    "index.html": "static/index.html",
    "styles.css": "static/styles.css",
    "app.js": "static/app.js",
    "runtime.js": "browser/runtime.js",
    "python-worker.js": "browser/python-worker.js",
    "archive.js": "browser/archive.js",
    "browser_api.py": "browser/browser_api.py",
    "archive_adapter.py": "archive_adapter.py",
    "engine.py": "engine.py",
    "importers.py": "importers.py",
    "reports.py": "reports.py",
    "sample-data.json": "sample-data.json",
    "guide.md": "materials/guide.md",
    "template.md": "materials/template.md",
    "THIRD_PARTY_NOTICES.md": "deployment/THIRD_PARTY_NOTICES.md",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects before making a request to an unallowlisted target."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("运行时下载发生重定向；已停止，未请求新地址。")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_runtime_lock(root: Path) -> dict:
    lock = json.loads((root / "deployment/runtime-lock.json").read_text(encoding="utf-8"))
    if lock.get("pyodide_version") != PYODIDE_VERSION or lock.get("python_version") != "3.12.7":
        raise ValueError("运行时版本与构建器不一致。")
    files = lock.get("files", {})
    if set(files) != set(RUNTIME_URLS):
        raise ValueError("运行时清单包含缺失或未经允许的文件。")
    for name, url in RUNTIME_URLS.items():
        entry = files[name]
        if entry.get("url") != url:
            raise ValueError(f"未经允许的运行时下载地址：{name}")
        if not isinstance(entry.get("bytes"), int) or not 0 < entry["bytes"] <= MAX_RUNTIME_FILE:
            raise ValueError(f"无效的运行时长度：{name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ValueError(f"无效的运行时校验值：{name}")
    return lock


def validate_runtime(content: bytes, name: str, entry: dict) -> None:
    if len(content) != entry["bytes"] or sha256(content) != entry["sha256"]:
        raise ValueError(f"运行时校验失败：{name}；请检查该缓存文件后重新获取。")


def runtime_files(root: Path, cache: Path, download: bool) -> tuple[dict, dict[str, bytes]]:
    lock = read_runtime_lock(root)
    result = {}
    opener = urllib.request.build_opener(NoRedirect())
    for name, entry in lock["files"].items():
        target = cache / name
        if target.is_symlink():
            raise ValueError(f"拒绝读取符号链接缓存：{name}")
        if target.exists():
            if target.stat().st_size != entry["bytes"]:
                raise ValueError(f"运行时缓存长度不匹配：{name}")
            content = target.read_bytes()
        elif download:
            request = urllib.request.Request(entry["url"], headers={"User-Agent": "originality-pages-builder/1"})
            with opener.open(request, timeout=60) as response:
                if response.status != 200 or response.url != entry["url"]:
                    raise ValueError(f"运行时响应不符合预期：{name}")
                content = response.read(entry["bytes"] + 1)
            validate_runtime(content, name, entry)
            cache.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            print(f"Downloaded and verified {name} ({len(content):,} bytes)")
        else:
            raise ValueError(f"缺少运行时 {name}；首次构建请添加 --download-runtime。")
        validate_runtime(content, name, entry)
        result[f"vendor/pyodide/{name}"] = content
    return lock, result


def render_html(source: str) -> str:
    """Make the same GUI work at /repository-name/ instead of only domain root."""
    for name in ("styles.css", "app.js", "runtime.js"):
        source = re.sub(
            rf'((?:href|src)=[\"\'])(?:/|\./)?{re.escape(name)}([\"\'])',
            rf'\g<1>./{name}\2', source,
        )
    if not re.search(r'<script\b[^>]*\bsrc=[\"\']\./runtime\.js[\"\']', source):
        app = re.search(r'<script\b[^>]*\bsrc=[\"\']\./app\.js[\"\'][^>]*>\s*</script>', source)
        if not app:
            raise ValueError("HTML 缺少应用脚本入口。")
        source = source[:app.start()] + '<script src="./runtime.js" defer></script>\n  ' + source[app.start():]
    first_script = re.search(r"<script\b", source)
    if not first_script or "ORIGINALITY_DEPLOYMENT" in source:
        raise ValueError("HTML 部署配置入口重复或缺失。")
    source = source[:first_script.start()] + '<script>window.ORIGINALITY_DEPLOYMENT = "browser";</script>\n  ' + source[first_script.start():]
    if re.search(r'(?:href|src)=[\"\']/(?!/)', source):
        raise ValueError("HTML 仍包含域名根目录资源路径，无法部署到仓库子路径。")
    for name in ("runtime.js", "app.js"):
        script = re.search(rf'<script\b[^>]*\bsrc=[\"\']\./{re.escape(name)}[\"\'][^>]*>', source)
        if not script or not re.search(r"\bdefer\b", script.group(0)):
            raise ValueError(f"HTML 脚本必须使用 defer：{name}")
    if source.index("./runtime.js") > source.index("./app.js"):
        raise ValueError("浏览器运行时必须先于应用脚本加载。")
    return source


def build_site(root: Path, output: Path, cache: Path, download: bool = False) -> dict:
    root = root.resolve()
    output = output.resolve()
    cache = cache.resolve()
    if root == output or root.is_relative_to(output):
        raise ValueError("输出目录不能覆盖源码目录或其父目录。")
    sources = {}
    for target, relative_source in SITE_SOURCES.items():
        source = root / relative_source
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"缺少构建所需源码：{relative_source}")
        sources[target] = source.read_bytes()
    sources["index.html"] = render_html(sources["index.html"].decode("utf-8-sig")).encode("utf-8")
    lock, runtimes = runtime_files(root, cache, download)
    sources.update(runtimes)
    sources[".nojekyll"] = b""
    manifest = {
        "deployment": "browser", "pyodide_version": lock["pyodide_version"],
        "python_version": lock["python_version"],
        "files": {name: {"bytes": len(content), "sha256": sha256(content)} for name, content in sorted(sources.items())},
    }
    sources["site-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Rebuild only an allowlisted artifact; never delete a directory or bundle unrelated data.
    if output.exists():
        for existing in output.rglob("*"):
            if existing.is_symlink() or (existing.is_file() and existing.relative_to(output).as_posix() not in sources):
                raise ValueError("输出目录包含非构建文件；请选择独立、空的输出目录。")
    output.mkdir(parents=True, exist_ok=True)
    for name, content in sources.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError("输出路径不能包含符号链接。")
        target.write_bytes(content)
    for name, content in sources.items():
        if (output / name).read_bytes() != content:
            raise ValueError(f"构建结果回读校验失败：{name}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the browser-only GitHub Pages application")
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--runtime-cache", type=Path, default=ROOT / "vendor/pyodide")
    parser.add_argument("--download-runtime", action="store_true", help="download only pinned HTTPS runtime files when absent")
    args = parser.parse_args()
    try:
        manifest = build_site(ROOT, args.output, args.runtime_cache, args.download_runtime)
    except (ValueError, OSError, urllib.error.URLError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    total = sum(item["bytes"] for item in manifest["files"].values())
    print(f"Built {len(manifest['files'])} allowlisted files ({total:,} bytes) at {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
