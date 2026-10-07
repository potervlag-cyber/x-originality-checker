"""Read X archive post files and summarize observed evidence, never pass odds.

The browser ZIP reader supplies only allowlisted post JSON and explicitly matched
media hashes. No archive JavaScript is executed and no account/DM file is needed.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from engine import analyze, _clean
from importers import ImportErrorDetail, MAX_ARCHIVE_POSTS, _safe_member, normalize_project

POST_FILE = re.compile(r"(?:tweets?|posts?)(?:[-_]part\d+)?\.(?:js|json)$", re.I)
ASSIGNMENT = re.compile(r"\s*window\.YTD\.(?:tweets?|posts?)(?:_part\d+)?\.part\d+\s*=\s*")
MAX_ARCHIVE_FILE_TEXT = 64 * 1024 * 1024
MAX_ARCHIVE_FILES_TEXT = 128 * 1024 * 1024
PROBABILITY_EXPLANATION = "X 未公开原创审核模型、样本权重或可校准的通过数据；仅凭归档无法可靠估算官方通过概率。"


def _id(value):
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _public_post_file(name):
    path = str(name).replace("\\", "/")
    parts = PurePosixPath(path).parts
    return _safe_member(path) and bool(POST_FILE.fullmatch(PurePosixPath(path).name)) and (
        len(parts) == 1 or "data" in parts[:-1]
    )


def _records(file):
    if not isinstance(file, dict) or not _public_post_file(file.get("name", "")):
        raise ImportErrorDetail("归档帖子文件路径不受支持；只读取 data 内的 tweets/posts 发帖文件。")
    text = file.get("text")
    if not isinstance(text, str):
        raise ImportErrorDetail("归档帖子文件应为文本。")
    if len(text.encode("utf-8")) > MAX_ARCHIVE_FILE_TEXT:
        raise ImportErrorDetail("单个归档帖子文件展开超过 64 MB；未进行部分分析。")
    if file["name"].casefold().endswith(".js"):
        text = text.lstrip("\ufeff")
        match = ASSIGNMENT.match(text)
        if not match:
            raise ImportErrorDetail("归档帖子文件不是受支持的 X JSON 赋值；不会执行 JavaScript。")
        text = text[match.end():].rstrip()
        if text.endswith(";"):
            text = text[:-1].rstrip()
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ImportErrorDetail("归档帖子 JSON 损坏或含有额外代码。") from exc
    if isinstance(value, dict):
        value = value.get("tweets", value.get("posts"))
    if not isinstance(value, list):
        raise ImportErrorDetail("归档帖子文件中未找到有效数组。")
    return value


def _long_text(raw):
    for key in ("note_tweet", "noteTweet"):
        note = raw.get(key)
        if isinstance(note, dict):
            if isinstance(note.get("text"), str):
                return note["text"], True
            result = note.get("note_tweet_results", note.get("noteTweetResults", {}))
            result = result.get("result", {}) if isinstance(result, dict) else {}
            if isinstance(result, dict) and isinstance(result.get("text"), str):
                return result["text"], True
    extended = raw.get("extended_tweet", {})
    if isinstance(extended, dict) and isinstance(extended.get("full_text"), str):
        return extended["full_text"], True
    text = raw.get("full_text")
    if text is None:
        text = raw.get("text", "")
    if not isinstance(text, str):
        raise ImportErrorDetail("归档中的帖子正文不是文本，无法完成全部帖子检查。")
    incomplete = raw.get("truncated") is True or any(raw.get(k) for k in (
        "note_tweet_id", "note_tweet_id_str", "note_tweet", "noteTweet", "is_longform"
    ))
    entities = raw.get("entities", {})
    if isinstance(entities, dict):
        for url in entities.get("urls", []) if isinstance(entities.get("urls", []), list) else []:
            if isinstance(url, dict) and re.search(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/", str(url.get("expanded_url", ""))):
                incomplete = True  # An article link does not supply the article body.
    return text, False if incomplete else (True if text else None)


def _media(raw, post_id):
    entities = raw.get("extended_entities")
    if not isinstance(entities, dict):
        entities = raw.get("entities", {})
    references = entities.get("media", []) if isinstance(entities, dict) else []
    if not isinstance(references, list):
        raise ImportErrorDetail("归档帖子媒体字段格式异常。")
    media, requests = [], []
    seen = set()
    for item in references:
        if not isinstance(item, dict):
            raise ImportErrorDetail("归档帖子媒体记录格式异常。")
        kind = item.get("type", "unknown")
        urls = []
        if kind in {"video", "animated_gif"}:
            info = item.get("video_info", {})
            variants = info.get("variants", []) if isinstance(info, dict) else []
            urls = [v.get("url", "") for v in variants if isinstance(v, dict) and (v.get("content_type") == "video/mp4" or str(v.get("url", "")).split("?", 1)[0].endswith(".mp4"))]
        if not urls:
            urls = [item.get("media_url_https", item.get("media_url", ""))]
        names = []
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            base = PurePosixPath(unquote(urlsplit(url).path)).name
            if base and base not in {".", ".."} and "/" not in base and "\\" not in base:
                name = f"{post_id}-{base}"
                if name not in names:
                    names.append(name)
        if not names:
            names = [f"{post_id}-media-{len(media) + 1}"]
        signature = tuple(names)
        if signature in seen:
            continue
        seen.add(signature)
        media.append({"name": names[0], "kind": "image" if kind == "photo" else kind,
                      "hash": "", "archive_names": names})
        requests.extend(names)
    return media, requests


def prepare_archive(post_files, metadata=None):
    """Parse every record; error on malformed records rather than silently skip."""
    if not isinstance(post_files, list) or not post_files:
        raise ImportErrorDetail("ZIP 中没有受支持的 tweets/posts 发帖归档文件。")
    posts, names, warnings = [], [], []
    raw_count, text_bytes = 0, 0
    parent_map = {}
    long_missing = 0
    for file in post_files:
        text_bytes += len(str(file.get("text", "")).encode("utf-8")) if isinstance(file, dict) else 0
        if text_bytes > MAX_ARCHIVE_FILES_TEXT:
            raise ImportErrorDetail("归档帖子文件展开总量超过 128 MB；未进行部分分析。")
        records = _records(file)
        raw_count += len(records)
        if raw_count > MAX_ARCHIVE_POSTS:
            raise ImportErrorDetail("归档超过 500000 条记录；未截取帖子或给出部分分析。")
        for wrapper in records:
            if not isinstance(wrapper, dict):
                raise ImportErrorDetail("归档含无效帖子记录；未跳过损坏记录进行部分分析。")
            raw = wrapper.get("tweet", wrapper.get("post", wrapper))
            if not isinstance(raw, dict):
                raise ImportErrorDetail("归档帖子对象格式异常。")
            post_id = _id(raw.get("id_str") or raw.get("id"))
            if not post_id:
                raise ImportErrorDetail("归档记录缺少帖子 ID，无法可靠关联全部正文与媒体。")
            text, complete = _long_text(raw)
            if complete is False:
                long_missing += 1
            parent = _id(raw.get("in_reply_to_status_id_str") or raw.get("in_reply_to_status_id"))
            quoted = _id(raw.get("quoted_status_id_str") or raw.get("quoted_status_id"))
            if raw.get("retweeted_status") or raw.get("retweeted_status_id_str") or re.match(r"^RT\s+@", text):
                kind = "repost"
            elif parent:
                kind = "reply"
            elif raw.get("is_quote_status") is True or quoted or raw.get("quoted_status_permalink"):
                kind = "quote"
            else:
                kind = "original"  # A record category, never verified original authorship.
            media, requests = _media(raw, post_id)
            names.extend(requests)
            parent_map[post_id] = parent
            posts.append({"id": post_id, "text": text, "text_complete": complete,
                          "type": kind, "reply_to": parent, "media": media,
                          "created_at": raw.get("created_at", ""),
                          "url": f"https://x.com/i/status/{post_id}"})
    if not posts:
        raise ImportErrorDetail("归档发帖数组为空，没有可分析的内容。")
    project = normalize_project({"posts": posts, "scope": {"timezone": "UTC", "complete": None,
        "selection": "上传归档中的全部发帖；不等于当前账号或历史全部帖子"}}, warnings, full_archive=True)
    # Save only the alternate media names lost by generic normalization.
    media_names = {(p["id"], p["text"].strip()): [m["archive_names"] for m in p["media"]] for p in posts}
    for post in project["posts"]:
        original_id = re.sub(r"__\d+$", "", post["id"])
        alternatives = media_names.get((original_id, post["text"]), [])
        for index, media in enumerate(post["media"]):
            media["archive_names"] = alternatives[index] if index < len(alternatives) else [media["name"]]
    thread_groups = _thread_groups(project["posts"], parent_map)
    if long_missing:
        warnings.append(f"{long_missing} 条记录标记为截断、长文引用或文章链接；发帖分片中未取得完整正文，结果保留此缺失。")
    parts = set()
    for file in post_files:
        match = re.search(r"[-_]part(\d+)\.(?:js|json)$", file["name"], re.I)
        parts.add(int(match.group(1)) if match else 0)
    if parts and (min(parts) != 0 or len(parts) != max(parts) + 1):
        warnings.append("归档帖子分片编号存在缺口；已检查实际提供的全部分片，无法确认缺少分片中的帖子。")
    meta = metadata if isinstance(metadata, dict) else {}
    safe_metadata = {k: v for k, v in meta.items() if k in {"input_bytes", "zip_entries", "public_post_files", "ignored_entries", "unread_note_files"} and isinstance(v, int) and v >= 0}
    if safe_metadata.get("unread_note_files"):
        warnings.append(f"归档存在 {safe_metadata['unread_note_files']} 个独立长帖文件，当前格式尚未解析关联，部分长文需补证；不能确认全部长帖正文完整。")
    return {"project": project, "media_names": list(dict.fromkeys(names)), "warnings": warnings,
            "metadata": safe_metadata, "post_files": [f["name"] for f in post_files],
            "archive_records": raw_count, "thread_groups": thread_groups}


def _thread_groups(posts, parents):
    """Union actual parent IDs present in this archive, with no time guess."""
    ids = {p["id"] for p in posts}
    roots = {post_id: post_id for post_id in ids}
    def find(value):
        while roots[value] != value:
            roots[value] = roots[roots[value]]
            value = roots[value]
        return value
    for post_id in ids:
        parent = parents.get(post_id, "")
        if parent in ids and parent != post_id:
            roots[find(post_id)] = find(parent)
    groups = defaultdict(list)
    for post_id in ids:
        groups[find(post_id)].append(post_id)
    return sum(len(members) > 1 for members in groups.values())


def finish_archive(prepared, media_result=None):
    project = prepared["project"]
    media_result = media_result if isinstance(media_result, dict) else {}
    hashes = {}
    for item in media_result.get("hashes", []):
        if isinstance(item, dict) and isinstance(item.get("name"), str) and re.fullmatch(r"[a-fA-F0-9]{64}", str(item.get("hash", ""))):
            hashes[item["name"]] = item["hash"].lower()
    for post in project["posts"]:
        for media in post["media"]:
            for name in media.get("archive_names", [media["name"]]):
                if name in hashes:
                    media.update(name=name, hash=hashes[name], hash_origin="local_file")
                    break
    result = analyze(project, full_archive=True, compact=True)
    assessed = result["posts"]
    # Count each affected post once, even when several evidence records match.
    codes = Counter()
    for post in assessed:
        codes.update({reason["code"] for reason in post["reasons"]})
    own_total = sum(post["type"] != "repost" for post in assessed)
    own = [post for post in assessed if post["type"] != "repost" and len(_clean(post["text"])) >= 24 and post["text_complete"] is True]
    duplicates = sum(any(r["code"] in {"internal_exact", "internal_near"} for r in p["reasons"]) for p in own)
    numerator = len(own) - duplicates
    coverage = result["coverage"]
    reasons = []
    for code, title, detail in (
        ("internal_exact", "账号内重复正文", "正文与归档内其他帖子完全或仅标点/链接不同；重复发布不等于抄袭，也不能证明来源归属。"),
        ("internal_near", "账号内相似正文", "发现高度相似的文字；模板、改写和本人重复发布需要结合上下文核实。"),
        ("repost_record", "普通转帖", "这些记录保留在总扫描数中，但不纳入本人正文的未重复比例；转帖本身不等于账号违规。"),
        ("quote_source_missing", "引用内容缺失", "归档未提供被引用正文与新增贡献证据，不能仅凭引用关系判断原创。"),
        ("text_incomplete", "正文不完整", "存在截断、长文引用或文章链接，需要完整正文才能评价。"),
        ("missing_text", "缺少可检查正文", "这些帖子的媒体或链接没有进行内容识别，不能凭空判为原创。"),
        ("media_reference_only", "归档媒体未匹配", "帖子引用了媒体，但未取得唯一匹配的归档文件。"),
        ("media_same_hash", "重复媒体文件", "匹配媒体与其他帖子文件 SHA-256 相同；这只能证明文件相同，不能判定作者归属。"),
        ("media_content_unchecked", "媒体画面待核实", "媒体文件已匹配并计算哈希；未检查图片、视频语义或全网来源。"),
        ("repeated_engagement_phrase", "重复互动请求", "多条帖子重复同一明确互动请求，需结合账号上下文核实。"),
    ):
        if codes[code]:
            reasons.append({"code": code, "title": title, "detail": detail, "count": codes[code]})
    if not reasons:
        reasons.append({"code": "no_internal_match", "title": "未发现明显账号内重复", "detail": "已提供的归档正文中没有发现明显重复信号；没有执行全网查重，不能据此证明原创。", "count": len(own)})
    if coverage["approximate_comparison_limited"]:
        reasons.append({"code": "comparison_budget", "title": "近似比对存在覆盖限制", "detail": "全部帖子已扫描并进行完全重复分组；近似文本比对触发预算或索引限制，未重复比例可能偏高。", "count": None})
    examples = []
    for status in ("high_risk", "insufficient", "review", "low_signal"):
        for post in assessed:
            if post["status"] != status or len(examples) >= 12:
                continue
            examples.append({"id": post["id"], "url": post["url"], "text": post["text"][:240], "status": status,
                             "status_label": post["status_label"], "reasons": post["reasons"][:4]})
    types = Counter(p["type"] for p in assessed)
    warnings = list(dict.fromkeys([*prepared["warnings"], *project["import_warnings"],
        *[str(w) for w in media_result.get("warnings", []) if isinstance(w, str)]]))
    missing_media = media_result.get("missing", [])
    if missing_media:
        warnings.append(f"{len(missing_media)} 个请求的媒体文件名未唯一匹配；不解析私信或账号资料。")
    meta = prepared["metadata"]
    return {"summary": {"total": len(assessed), "types": {"posts": types["original"] + types["article"], "reply": types["reply"], "repost": types["repost"], "quote": types["quote"]},
        "counts": result["summary"]["counts"], "official_probability": None, "probability_explanation": PROBABILITY_EXPLANATION,
        "nonduplicate_percent": round(numerator / len(own) * 100, 1) if own else None,
        "nonduplicate_numerator": numerator, "nonduplicate_denominator": len(own),
        "comparison_excluded_posts": own_total - len(own),
        "exact_duplicate_posts": codes["internal_exact"], "near_duplicate_posts": codes["internal_near"],
        "missing_text_posts": codes["missing_text"], "incomplete_text_posts": codes["text_incomplete"],
        "media_references": coverage["media_references"], "hashed_media": coverage["hashed_media"],
        "media_completeness_percent": round(coverage["hashed_media"] / coverage["media_references"] * 100, 1) if coverage["media_references"] else None,
        "thread_groups": prepared["thread_groups"], "analyzed_all_archive_posts": True},
        "reasons": reasons, "examples": examples, "coverage": {
            "actual_start": coverage["actual_start"], "actual_end": coverage["actual_end"], "post_files": len(prepared["post_files"]), "post_file_names": prepared["post_files"],
            "archive_records": prepared["archive_records"], "analyzed_posts": len(assessed),
            "zip_entries": meta.get("zip_entries"), "compressed_bytes": meta.get("input_bytes"),
            "approximate_comparison_limited": coverage["approximate_comparison_limited"],
            "near_comparisons": coverage["near_comparisons"], "notes": [
                "扫描范围是 ZIP 中全部受支持的发帖文件，包含归档中的主帖、回复、普通转帖及引用；无法据此确认每条帖子的可见性。已删除帖子、未导出的长文及归档之外的内容无法核查。",
                "主帖分类表示没有明确回复、转帖或引用标记，不表示已确认原创；归档缺少引用字段时无法识别全部引用帖。",
                "线程按归档中实际存在的父帖子 ID 关联；不能证明线程没有删除或缺失分段。",
                "未重复比例 = 未发现账号内完全/高度相似正文的可比较记录 ÷ 可比较记录数；可比较记录为完整且去掉链接/标点后至少 24 字符的非普通转帖正文，包括回复及引用。缺少正文、短帖和不完整长帖不参与此比例，另列为无法比较，不是原创通过概率。",
                *coverage["notes"]]},
        "warnings": warnings, "limitations": [PROBABILITY_EXPLANATION,
            "不访问 X、来源链接或其他网站，未做全网查重；他人文字搬运可能没有账号内重复信号。",
            "文件哈希只比较相同字节，不识别媒体画面、作者归属、授权或语义改写。",
            "不判断会员、展示量、认证粉丝、账号处罚或收益分成的其他门槛。"]}
