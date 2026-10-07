"""JSON-only adapter for the trusted Python modules inside a browser worker.

No HTTP server, filesystem reads, network requests, or token exchange are needed.
The host passes a JSON string and receives a JSON string, never a live PyProxy.
"""
from __future__ import annotations

import base64
import binascii
import copy
import ipaddress
import json
from urllib.parse import urlsplit
import uuid

from engine import VERSION, _clean, analyze
from importers import MAX_FILE_BYTES, import_data, normalize_project
from reports import html_report, markdown_report
from archive_adapter import prepare_archive, finish_archive
from policy_checks import assess_policy

MAX_REQUEST_BYTES = 45 * 1024 * 1024
_prepared_archive = None
_retained_archive = None
WEB_STATUSES = {"matched", "no_match", "partial", "failed", "skipped"}


def _integer(value, label, minimum=0, maximum=1000000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label}格式或范围不正确。")
    return value


def _text(value, label, maximum, default=""):
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}格式或长度不正确。")
    return value


def _source_url(value):
    value = _text(value, "来源链接", 4096)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value) or parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError
        parsed.port  # Reject malformed ports as well as missing/ambiguous hosts.
        host = host.rstrip(".").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")) or "." not in host and ":" not in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Legacy numeric IPv4 syntax may be interpreted as loopback by browsers.
            if all(part.isdigit() or part.startswith("0x") for part in host.split(".")):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except (ValueError, TypeError):
        raise ValueError("来源链接必须为公开 HTTP(S) 地址，不能含账号口令或本机/私有地址。") from None
    return value


def _web_coverage(state):
    posts = list(state["web_posts"].values())
    searched = sum(p["successful_queries"] > 0 for p in posts)
    failed = sum(p["status"] == "failed" for p in posts)
    skipped = sum(p["status"] == "skipped" for p in posts)
    matched = sum(bool(p["matches"]) for p in posts)
    total = len(state["eligible"])
    return {"requested": len(posts), "searched": searched,
            "compared": sum(p["sources_checked"] > 0 for p in posts),
            "failed": failed, "skipped": skipped, "matched": matched,
            "partial": sum(p["status"] == "partial" for p in posts),
            "remaining": max(total - len(posts), 0), "total_eligible": total,
            "matched_percent": round(matched / searched * 100, 1) if searched else None,
            "text_truncated_posts": sum(p["text_truncated"] for p in posts),
            "checked_chars": sum(p["checked_chars"] for p in posts),
            "original_chars": sum(p["original_chars"] for p in posts),
            "search_complete": bool(posts) and len(posts) == total and not any(p["status"] in {"partial", "failed", "skipped"} or p["text_truncated"] or p["checked_chars"] < p["original_chars"] for p in posts),
            "web_coverage": "unknown"}


def _web_check(state):
    coverage = _web_coverage(state)
    return {"schema_version": 1,
            "status": "not_started" if not state["web_posts"] else "partial" if not coverage["search_complete"] else "completed",
            "provider": state.get("provider", ""), "checked_at": state.get("checked_at", ""),
            "coverage": coverage, "posts": list(state["web_posts"].values()),
            "limitations": ["公开搜索收录和网页访问有缺口，实际全网覆盖未知；未发现匹配不能证明原创。",
                            "文字重合不能确认作者归属、授权、自转载或引用是否恰当，需人工复核。",
                            "联网检查不重算离线参考概率，也不调用 X 官方审核或检查媒体来源。"]}


def _current_archive():
    if _retained_archive is None:
        raise ValueError("没有可联网检查的归档，请先完成 ZIP 分析。")
    return _retained_archive


def _web_plan(data):
    state = _current_archive()
    offset = _integer(data.get("offset", 0), "检查起点", maximum=len(state["eligible"]))
    limit = _integer(data.get("limit", 10), "每批帖子数", 1, 10)
    maximum = _integer(data.get("max_chars", 5000), "每条检索文字上限", 24, 5000)
    posts = []
    for post in state["eligible"][offset:offset + limit]:
        text = post["text"][:maximum]
        plan = {"id": post["id"], "text": text, "url": post["url"], "created_at": post["created_at"],
                "original_chars": len(post["text"]), "text_truncated": len(text) < len(post["text"])}
        state["planned"][post["id"]] = {"original_chars": plan["original_chars"], "text_truncated": plan["text_truncated"], "checked_chars": len(text)}
        posts.append(plan)
    next_offset = offset + len(posts)
    return {"session_id": state["session_id"], "total_eligible": len(state["eligible"]),
            "offset": offset, "next_offset": next_offset, "posts": posts, "done": next_offset >= len(state["eligible"])}


def _validated_report(report, state):
    if not isinstance(report, dict) or isinstance(report.get("schema_version"), bool) or report.get("schema_version") != 1 or not isinstance(report.get("posts"), list) or not 1 <= len(report["posts"]) <= 10:
        raise ValueError("联网报告应包含本批 1–10 条帖子结果。")
    provider = _text(report.get("provider"), "搜索提供方", 200)
    checked_at = _text(report.get("checked_at"), "检查时间", 100)
    posts, ids = [], set()
    for raw in report["posts"]:
        if not isinstance(raw, dict):
            raise ValueError("联网报告中的帖子结果格式异常。")
        post_id = _text(raw.get("id"), "帖子编号", 256)
        if post_id not in state["planned"] or post_id in ids:
            raise ValueError("联网报告包含未提交检索或重复的帖子编号。")
        ids.add(post_id)
        status = raw.get("status")
        if status not in WEB_STATUSES:
            raise ValueError("联网帖子检查状态不受支持。")
        matches = raw.get("matches", [])
        issues = raw.get("issues", [])
        if not isinstance(matches, list) or len(matches) > 3 or not isinstance(issues, list) or len(issues) > 20:
            raise ValueError("联网证据数量或格式异常。")
        validated_matches = []
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("联网匹配证据格式异常。")
            score = match.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise ValueError("联网匹配分数应在 0–1 范围内。")
            relation = match.get("temporal_relation", "unknown")
            if relation not in {"earlier", "later", "same", "unknown"}:
                raise ValueError("来源时间关系不受支持。")
            if match.get("source_kind") not in {"page_body", "search_snippet"}:
                raise ValueError("来源正文证据类型不受支持。")
            if match.get("source_text_truncated") is not None and not isinstance(match.get("source_text_truncated"), bool):
                raise ValueError("来源正文截断标记格式异常。")
            validated_matches.append({"url": _source_url(match.get("url")),
                "title": _text(match.get("title"), "来源标题", 1000), "score": score,
                "matched_chars": _integer(match.get("matched_chars", 0), "重合字符数", maximum=5000),
                "post_excerpt": _text(match.get("post_excerpt"), "帖子重合片段", 5000),
                "source_excerpt": _text(match.get("source_excerpt"), "来源重合片段", 5000),
                "published_at": _text(match.get("published_at"), "来源时间", 100),
                "published_at_basis": _text(match.get("published_at_basis"), "来源时间依据", 200),
                "temporal_relation": relation, "source_kind": _text(match.get("source_kind"), "来源类型", 100),
                "page_status": _text(match.get("page_status"), "来源网页状态", 100),
                "source_text_truncated": match.get("source_text_truncated")})
        if status in {"failed", "skipped", "no_match"} and validated_matches:
            raise ValueError("联网帖子状态与匹配证据不一致。")
        if status == "matched" and not validated_matches:
            raise ValueError("发现匹配状态需要提供来源证据。")
        matches_total = _integer(raw.get("matches_total", len(validated_matches)), "全部匹配数", minimum=len(validated_matches), maximum=1000)
        if status in {"failed", "skipped", "no_match"} and matches_total:
            raise ValueError("联网帖子状态与匹配总数不一致。")
        query_count = _integer(raw.get("query_count", 0), "检索次数", maximum=100)
        successful_queries = _integer(raw.get("successful_queries", 0), "成功检索次数", maximum=query_count)
        same_post = raw.get("same_post", [])
        if not isinstance(same_post, list) or len(same_post) > 30 or not all(isinstance(item, dict) and item.get("reason") == "same_post" for item in same_post):
            raise ValueError("本人原帖来源列表格式异常。")
        if not all(isinstance(issue, dict) for issue in issues):
            raise ValueError("联网检查提示格式异常。")
        checked_chars = _integer(raw.get("checked_chars", state["planned"][post_id]["checked_chars"]), "实际核查字符数", maximum=state["planned"][post_id]["checked_chars"])
        source_truncated = raw.get("text_truncated", False)
        if not isinstance(source_truncated, bool):
            raise ValueError("检索正文截断标记格式异常。")
        maximum_similarity = raw.get("max_similarity")
        if maximum_similarity is not None and (isinstance(maximum_similarity, bool) or not isinstance(maximum_similarity, (int, float)) or not 0 <= maximum_similarity <= 1):
            raise ValueError("联网最高重合分数格式异常。")
        text_truncated = state["planned"][post_id]["text_truncated"] or source_truncated
        if status in {"matched", "no_match"} and (text_truncated or checked_chars < state["planned"][post_id]["original_chars"]):
            status = "partial"
        posts.append({"id": post_id, "status": status,
            "query_count": query_count, "successful_queries": successful_queries,
            "candidates_found": _integer(raw.get("candidates_found", 0), "候选来源数", maximum=1000),
            "sources_checked": _integer(raw.get("sources_checked", 0), "核对来源数", maximum=1000),
            "matches": validated_matches, "matches_total": matches_total,
            "same_post": [{"url": _source_url(item.get("url")), "title": _text(item.get("title"), "本人原帖标题", 1000), "reason": "same_post"} for item in same_post],
            "issues": [{"code": _text(issue.get("code"), "检查提示代码", 100)} for issue in issues],
            "original_chars": state["planned"][post_id]["original_chars"], "checked_chars": checked_chars,
            "text_truncated": text_truncated,
            "max_similarity": maximum_similarity})
    return posts, provider, checked_at


def _web_apply(data):
    state = _current_archive()
    if data.get("session_id") != state["session_id"]:
        raise ValueError("这份联网报告不属于当前归档，请重新开始检查。")
    posts, provider, checked_at = _validated_report(data.get("report"), state)
    # Validate the complete batch before updating retained state. Bad evidence never
    # replaces an earlier result or prevents downloading the offline analysis.
    next_state = {**state, "web_posts": {**state["web_posts"], **{p["id"]: p for p in posts}},
                  "provider": provider or state.get("provider", ""), "checked_at": checked_at or state.get("checked_at", "")}
    web_check = _web_check(next_state)
    result = copy.deepcopy(state["base_result"])
    result["web_check"] = web_check
    result["coverage"]["web_check"] = web_check["coverage"]
    result["policy_checks"] = assess_policy(state["posts"], state["assessed_posts"], web_check)
    result["limitations"] = [item for item in result.get("limitations", []) if "未做全网查重" not in item]
    result["limitations"].extend(web_check["limitations"])
    for reason in result.get("reasons", []):
        if reason.get("code") == "no_internal_match":
            reason["detail"] = "已提供的归档正文中没有发现明显账号内重复信号；公开网络检查的覆盖与证据见联网结果，仍不能据此证明原创。"
    for factor in result.get("probability_factors", []):
        if factor.get("code") == "external_sources_unknown":
            factor["detail"] = "上方概率保留联网前的归档规则估计；本次公开来源检查不重算概率。全网覆盖和作者归属仍未知，联网证据见下方结果。"
    for example in result.get("examples", []):
        for reason in example.get("reasons", []):
            if reason.get("code") == "no_supplied_text_match":
                reason["message"] = "归档内比较未发现明显重复；本条是否联网及来源证据见联网报告，未发现匹配不能证明原创。"
    state.update(web_posts=next_state["web_posts"], provider=next_state["provider"], checked_at=next_state["checked_at"])
    return result


def prepare_archive_json(payload_json):
    """Keep the full archive in the worker; return only requested media names."""
    global _prepared_archive
    clear_archive()
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
    global _prepared_archive, _retained_archive
    _retained_archive = None
    try:
        if _prepared_archive is None:
            raise ValueError("尚未准备归档帖子。")
        media = json.loads(media_json)
        if not isinstance(media, dict):
            raise ValueError("归档媒体结果格式异常。")
        result = finish_archive(_prepared_archive, media)
        posts = _prepared_archive["project"]["posts"]
        _retained_archive = {"session_id": uuid.uuid4().hex, "posts": posts,
            "assessed_posts": _prepared_archive.get("_assessed_posts", posts),
            "base_result": result,
            "eligible": [p for p in posts if p["type"] != "repost" and p["text_complete"] is True and len(_clean(p["text"])) >= 24],
            "planned": {}, "web_posts": {}}
        result["web_check"] = _web_check(_retained_archive)
        response = {"ok": True, "result": result}
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        _retained_archive = None
        response = {"ok": False, "error": str(exc)}
    except Exception:
        _retained_archive = None
        response = {"ok": False, "error": "归档分析失败；未给出部分分析结果。"}
    finally:
        _prepared_archive = None
    return json.dumps(response, ensure_ascii=False)


def clear_prepared_archive():
    global _prepared_archive
    _prepared_archive = None


def clear_archive():
    global _retained_archive
    clear_prepared_archive()
    _retained_archive = None


def _dispatch(path, data):
    if not isinstance(data, dict):
        raise ValueError("请求需要包含对象。")
    if path == "/api/health":
        return {"ok": True, "app": "x-originality-checker", "version": VERSION, "runtime": "browser"}
    if path == "/api/archive/clear":
        clear_archive()
        return {"cleared": True}
    if path == "/api/webcheck/plan":
        return _web_plan(data)
    if path == "/api/webcheck/apply":
        return _web_apply(data)
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
