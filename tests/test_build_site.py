"""Exercise the artifact boundary, pinned downloads, and repository subpath HTML."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_site


class PagesArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.cache = self.root / "vendor/pyodide"
        self.cache.mkdir(parents=True)
        self.output = self.base / "pages-preview/x-originality-checker"
        for target, source in build_site.SITE_SOURCES.items():
            path = self.root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            content = "<!doctype html><head><link href='/styles.css'><script src='/app.js' defer></script></head>" if target == "index.html" else f"public fixture for {target}"
            path.write_text(content, encoding="utf-8")
        self.lock = {"pyodide_version": "0.27.7", "python_version": "3.12.7", "files": {}}
        for name, url in build_site.RUNTIME_URLS.items():
            content = ("pinned fixture " + name).encode()
            (self.cache / name).write_bytes(content)
            self.lock["files"][name] = {"url": url, "bytes": len(content), "sha256": build_site.sha256(content)}
        self.save_lock()

    def save_lock(self):
        path = self.root / "deployment/runtime-lock.json"
        path.write_text(json.dumps(self.lock), encoding="utf-8")

    def test_artifact_is_allowlisted_subpath_safe_and_repeatable(self):
        private = self.root / "qa/private-user-report.json"
        private.parent.mkdir()
        private.write_text('{"private":"never publish"}')
        with patch.object(build_site.urllib.request, "build_opener") as opener:
            manifest = build_site.build_site(self.root, self.output, self.cache)
            first = (self.output / "site-manifest.json").read_bytes()
            second_manifest = build_site.build_site(self.root, self.output, self.cache)
            opener.return_value.open.assert_not_called()
        self.assertEqual(manifest, second_manifest)
        self.assertEqual(first, (self.output / "site-manifest.json").read_bytes())
        self.assertFalse((self.output / "qa").exists())
        html = (self.output / "index.html").read_text()
        self.assertNotIn("src='/", html)
        self.assertNotIn("href='/", html)
        self.assertLess(html.index("ORIGINALITY_DEPLOYMENT"), html.index("./runtime.js"))
        self.assertLess(html.index("./runtime.js"), html.index("./app.js"))
        self.assertEqual(set(manifest["files"]), set(build_site.SITE_SOURCES) | {"vendor/pyodide/" + name for name in build_site.RUNTIME_URLS} | {".nojekyll"})

    def test_unknown_output_file_is_preserved_and_build_is_rejected(self):
        self.output.mkdir(parents=True)
        private = self.output / "user-material.json"
        private.write_text("private material")
        with self.assertRaisesRegex(ValueError, "非构建文件"):
            build_site.build_site(self.root, self.output, self.cache)
        self.assertEqual("private material", private.read_text())
        self.assertFalse((self.output / "index.html").exists())

    def test_source_or_parent_cannot_be_output(self):
        for output in (self.root, self.base):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "覆盖源码"):
                    build_site.build_site(self.root, output, self.cache)

    def test_corrupt_cache_is_rejected_before_writing_site(self):
        path = self.cache / "pyodide.js"
        content = path.read_bytes()
        path.write_bytes(b"x" * len(content))
        with self.assertRaisesRegex(ValueError, "校验失败"):
            build_site.build_site(self.root, self.output, self.cache)
        self.assertFalse(self.output.exists())

    def test_unknown_download_host_is_rejected_without_network(self):
        self.lock["files"]["pyodide.js"]["url"] = "https://example.invalid/pyodide.js"
        self.save_lock()
        with patch.object(build_site.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "未经允许"):
                build_site.build_site(self.root, self.output, self.cache, True)
            opener.assert_not_called()

    def test_extra_runtime_file_is_rejected(self):
        self.lock["files"]["third-party.whl"] = {}
        self.save_lock()
        with self.assertRaisesRegex(ValueError, "未经允许"):
            build_site.read_runtime_lock(self.root)

    def test_missing_runtime_does_not_fetch_unless_requested(self):
        (self.cache / "pyodide.js").unlink()
        with patch.object(build_site.urllib.request, "build_opener") as opener:
            with self.assertRaisesRegex(ValueError, "download-runtime"):
                build_site.build_site(self.root, self.output, self.cache)
            opener.return_value.open.assert_not_called()

    def test_redirect_is_refused_before_requesting_new_endpoint(self):
        with self.assertRaisesRegex(ValueError, "未请求新地址"):
            build_site.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid/")

    def test_other_root_absolute_resource_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "根目录资源路径"):
            build_site.render_html('<link href="/unlisted.css"><script src="./runtime.js" defer></script><script src="./app.js" defer></script>')

    def test_runtime_and_application_order_cannot_be_inverted(self):
        with self.assertRaisesRegex(ValueError, "先于应用"):
            build_site.render_html('<script src="./app.js" defer></script><script src="./runtime.js" defer></script>')


if __name__ == "__main__":
    unittest.main()
