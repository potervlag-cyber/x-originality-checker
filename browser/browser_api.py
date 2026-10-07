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
import re
from urllib.parse import urlsplit
import uuid

from engine import VERSION, _clean, analyze
from importers import MAX_FILE_BYTES, import_data, normalize_project
from reports import html_report, markdown_report
from archive_adapter import prepare_archive, finish_archive
from policy_checks import assess_policy, combined_evidence

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
    selected_total = len(state["selected"])
    complete = not any(p["status"] in {"partial", "failed", "skipped"} or p["text_truncated"] or p["checked_chars"] < p["original_chars"] for p in posts)
    return {"requested": len(posts), "searched": searched,
            "compared": sum(p["sources_checked"] > 0 for p in posts),
            "failed": failed, "skipped": skipped, "matched": matched,
            "partial": sum(p["status"] == "partial" for p in posts),
            "remaining": max(selected_total - len(posts), 0), "total_eligible": total,
            "selected_total": selected_total, "unselected_eligible": total - selected_total,
            "mode": state["selection_mode"], "sample_method": state["sample_method"],
            "selection_configured": state["selection_locked"],
            "selection_complete": len(posts) == selected_total,
            "selection_search_complete": bool(posts) and len(posts) == selected_total and complete,
            "all_eligible_requested": len(posts) == total,
            "execution_unknown_posts": sum(p.get("execution_status") == "unknown" for p in posts),
            "unknown_execution_chars": sum(p["original_chars"] for p in posts if p.get("execution_status") == "unknown"),
            "matched_percent": round(matched / searched * 100, 1) if searched else None,
            "text_truncated_posts": sum(p["text_truncated"] for p in posts),
            "checked_chars": sum(p["checked_chars"] for p in posts),
            "original_chars": sum(p["original_chars"] for p in posts),
            "search_complete": bool(posts) and len(posts) == total and complete,
            "web_coverage": "unknown"}


def _web_check(state):
    coverage = _web_coverage(state)
    return {"schema_version": 1,
            "status": "not_started" if not state["web_posts"] else "partial" if not coverage["selection_search_complete"] else "completed",
            "provider": state.get("provider", ""), "checked_at": state.get("checked_at", ""),
            "coverage": coverage, "posts": list(state["web_posts"].values()),
            "limitations": ["公开搜索收录和网页访问有缺口，实际全网覆盖未知；未发现匹配不能证明原创。",
                            "文字重合不能确认作者归属、授权、自转载或引用是否恰当，需人工复核。",
                            "联网仅检查手动链接指定的 10 条归档正文，不抓取 X 链接、不补抽帖子；结果不能推算未选帖子或全归档的原创程度。" if state["selection_mode"] == "manual10" else "本次尚未选择联网帖子；归档本地分析不代表已检索公开来源。",
                            "批次发出后取消、超时或响应无效时，上游是否执行及费用未知；返回的零成功次数只表示未取得证据，不表示没有检索消耗。",
                            "联网检查不重算离线参考概率，也不调用 X 官方审核或检查媒体来源。"]}


def _current_archive():
    if _retained_archive is None:
        raise ValueError("没有可联网检查的归档，请先完成 ZIP 分析。")
    return _retained_archive


def _manual_ids(links):
    if not isinstance(links, list) or len(links) != 10:
        raise ValueError("请完整填写 10 个不同的 X 帖子链接。")
    ids = []
    for index, value in enumerate(links, 1):
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError
            value = value.strip()
            parsed = urlsplit(value)
            if (any(ord(char) < 33 or char == "\\" or ord(char) == 127 for char in value)
                    or parsed.scheme not in {"http", "https"}
                    or (parsed.hostname or "").lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}):
                raise ValueError
            match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([1-9][0-9]{0,29})(?:/(?:photo|video)/[1-9][0-9]*)?/?", parsed.path)
            if not match:
                raise ValueError
            pid = match[1]
        except (ValueError, TypeError):
            raise ValueError(f"第 {index} 个链接不是有效的 X/twitter 数字编号帖子链接。") from None
        if pid in ids:
            raise ValueError(f"第 {index} 个链接与前面填写的帖子重复，请填写 10 条不同帖子。")
        ids.append(pid)
    return ids


def _web_plan(data):
    state = _current_archive()
    mode = data.get("mode", state["selection_mode"])
    if mode != "manual10":
        raise ValueError("联网分析仅支持手动填写 10 条 X 帖子链接。")
    if len(state["eligible"]) < 10:
        raise ValueError("归档中不足 10 条完整且可检索的非转帖正文，无法执行 10 条联网分析；仍可使用本地分析。")
    selected = state["selected"]
    if not state["selection_locked"] or "links" in data:
        ids = _manual_ids(data.get("links"))
        eligible = {post["id"]: post for post in state["eligible"]}
        for index, pid in enumerate(ids, 1):
            if pid not in eligible:
                raise ValueError(f"第 {index} 个链接未匹配归档中可检索的完整非转帖正文；不会抓取链接或自动换选帖子。")
        if state["selection_locked"] and ids != [post["id"] for post in selected]:
            raise ValueError("当前归档的 10 条联网选择已固定；请重新导入归档后再更换链接。")
        selected = [eligible[pid] for pid in ids]
    offset = _integer(data.get("offset", 0), "检查起点", maximum=len(selected))
    limit = _integer(data.get("limit", 10), "每批帖子数", 1, 10)
    maximum = _integer(data.get("max_chars", 5000), "每条检索文字上限", 24, 5000)
    state.update(selected=selected, selection_mode=mode, selection_locked=True,
                 sample_method="manual_x_status_links")
    posts = []
    cursor = offset
    while cursor < len(selected) and len(posts) < limit:
        post = selected[cursor]
        cursor += 1
        if post["id"] in state["web_posts"]:
            continue
        text = post["text"][:maximum]
        plan = {"id": post["id"], "text": text, "url": post["url"], "created_at": post["created_at"],
                "original_chars": len(post["text"]), "text_truncated": len(text) < len(post["text"])}
        state["planned"][post["id"]] = {"original_chars": plan["original_chars"], "text_truncated": plan["text_truncated"], "checked_chars": len(text)}
        posts.append(plan)
    next_offset = cursor
    return {"session_id": state["session_id"], "total_eligible": len(state["eligible"]),
            "selected_total": len(state["selected"]), "mode": state["selection_mode"], "sample_method": state["sample_method"],
            "offset": offset, "next_offset": next_offset, "remaining": len(state["selected"]) - next_offset,
            "posts": posts, "done": next_offset >= len(state["selected"])}


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
            "max_similarity": maximum_similarity, "execution_status": "reported"})
    return posts, provider, checked_at


def _composed_result(state, next_state):
    web_check = _web_check(next_state)
    result = copy.deepcopy(state["base_result"])
    result["web_check"] = web_check
    result["coverage"]["web_check"] = web_check["coverage"]
    result["policy_checks"] = assess_policy(state["posts"], state["assessed_posts"], web_check)
    result["summary"]["combined_evidence"] = combined_evidence(result["summary"], result["policy_checks"], web_check, state["posts"])
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


def _web_apply(data):
    state = _current_archive()
    if data.get("session_id") != state["session_id"]:
        raise ValueError("这份联网报告不属于当前归档，请重新开始检查。")
    posts, provider, checked_at = _validated_report(data.get("report"), state)
    # Validate and compose the complete batch before updating retained state.
    next_state = {**state, "web_posts": {**state["web_posts"], **{p["id"]: p for p in posts}},
                  "provider": provider or state.get("provider", ""), "checked_at": checked_at or state.get("checked_at", "")}
    return _composed_result(state, next_state)


def _web_abandon(data):
    state = _current_archive()
    if data.get("session_id") != state["session_id"]:
        raise ValueError("这份联网批次不属于当前归档。")
    ids = data.get("ids")
    reason = data.get("reason")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(pid, str) for pid in ids)
            or len(set(ids)) != len(ids) or any(pid not in state["planned"] for pid in ids)
            or reason not in {"response_unknown", "cancelled"}):
        raise ValueError("无法确认当前已发出的联网批次。")
    updates = {}
    for pid in ids:
        if pid in state["web_posts"]:
            continue  # Never overwrite evidence that arrived before cancellation.
        plan = state["planned"][pid]
        updates[pid] = {"id": pid, "status": "failed", "execution_status": "unknown",
            "query_count": 0, "successful_queries": 0, "candidates_found": 0, "sources_checked": 0,
            "matches": [], "matches_total": 0, "same_post": [],
            "issues": [{"code": "batch_response_unknown"}, {"code": reason}],
            "original_chars": plan["original_chars"], "checked_chars": 0,
            "text_truncated": plan["text_truncated"], "max_similarity": None}
    next_state = {**state, "web_posts": {**state["web_posts"], **updates},
                  "provider": state.get("provider", ""), "checked_at": state.get("checked_at", "")}
    return _composed_result(state, next_state)


def _web_result():
    state = _current_archive()
    return _composed_result(state, {**state, "provider": state.get("provider", ""), "checked_at": state.get("checked_at", "")})


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
        _retained_archive.update(selected=[], selection_mode="not_selected", sample_method="not_selected", selection_locked=False)
        result["web_check"] = _web_check(_retained_archive)
        result["summary"]["combined_evidence"] = combined_evidence(result["summary"], result["policy_checks"], result["web_check"], posts)
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
    if path == "/api/webcheck/abandon":
        return _web_abandon(data)
    if path == "/api/webcheck/result":
        return _web_result()
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
