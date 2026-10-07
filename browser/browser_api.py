"""JSON-only adapter for the trusted Python modules inside a browser worker.

No HTTP server, filesystem reads, network requests, or token exchange are needed.
The host passes a JSON string and receives a JSON string, never a live PyProxy.
"""
from __future__ import annotations

import base64
import binascii
import json

from engine import VERSION, analyze
from importers import MAX_FILE_BYTES, import_data, normalize_project
from reports import html_report, markdown_report
from archive_adapter import prepare_archive, finish_archive

MAX_REQUEST_BYTES = 45 * 1024 * 1024
_prepared_archive = None


def prepare_archive_json(payload_json):
    """Keep the full archive in the worker; return only requested media names."""
    global _prepared_archive
    _prepared_archive = None
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise ValueError("归档请求需要包含对象。")
        _prepared_archive = prepare_archive(payload.get("files"), payload.get("metadata"))
        response = {"ok": True, "result": {"media_names": _prepared_archive["media_names"]}}
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        response = {"ok": False, "error": "归档帖子解析失败；未进行部分分析。"}
    return json.dumps(response, ensure_ascii=False)


def finish_archive_json(media_json):
    global _prepared_archive
    try:
        if _prepared_archive is None:
            raise ValueError("尚未准备归档帖子。")
        media = json.loads(media_json)
        if not isinstance(media, dict):
            raise ValueError("归档媒体结果格式异常。")
        response = {"ok": True, "result": finish_archive(_prepared_archive, media)}
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        response = {"ok": False, "error": "归档分析失败；未给出部分分析结果。"}
    finally:
        _prepared_archive = None
    return json.dumps(response, ensure_ascii=False)


def clear_archive():
    global _prepared_archive
    _prepared_archive = None


def _dispatch(path, data):
    if not isinstance(data, dict):
        raise ValueError("请求需要包含对象。")
    if path == "/api/health":
        return {"ok": True, "app": "x-originality-checker", "version": VERSION, "runtime": "browser"}
    if path == "/api/import":
        encoded = data.get("content_base64", "")
        if not isinstance(encoded, str) or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 8:
            raise ValueError("单个材料文件最多 30 MB。")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("单个材料文件最多 30 MB。")
        return import_data(content, str(data.get("filename", "")))
    if path in {"/api/analyze", "/api/report"}:
        project = normalize_project(data.get("project", {}))
        result = analyze(project)
        if path == "/api/analyze":
            return {"project": project, "analysis": result}
        report_format = data.get("format", "markdown")
        if report_format not in {"html", "markdown"}:
            raise ValueError("报告格式应为 html 或 markdown。")
        content = html_report(project, result) if report_format == "html" else markdown_report(project, result)
        return {"content": content, "format": report_format}
    raise ValueError("接口不存在。")


def dispatch_json(request_json):
    """Return {ok,result} or {ok,error} as a JSON string for one request."""
    try:
        if not isinstance(request_json, str):
            raise ValueError("请求必须为 JSON 文本。")
        if len(request_json) > MAX_REQUEST_BYTES or len(request_json.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("材料过大，单次请求最多 45 MB。请分批导入。")
        request = json.loads(request_json)
        if not isinstance(request, dict):
            raise ValueError("请求需要包含对象。")
        response = {"ok": True, "result": _dispatch(request.get("path"), request.get("data", {}))}
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError, OverflowError) as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        # Never expose a Python stack, paths, or unexpected submitted content.
        response = {"ok": False, "error": "处理材料时遇到内部错误。请检查材料格式，或重新加载页面后重试。"}
    return json.dumps(response, ensure_ascii=False)
