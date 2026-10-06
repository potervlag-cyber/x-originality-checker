"""Local, bounded import of supplied public-post material. Never executes archive JS."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_POSTS = 2000
MAX_TEXT_LENGTH = 50000
MAX_TOTAL_TEXT = 5 * 1024 * 1024


class ImportErrorDetail(ValueError):
    """An input error safe to show in the interface."""


ALIASES = {
    "id": ["id", "id_str", "post_id", "tweet_id", "帖子标识", "帖子编号", "编号"],
    "url": ["url", "link", "post_url", "tweet_url", "帖子链接", "链接"],
    "text": ["text", "full_text", "content", "body", "正文", "帖子原文", "帖子正文", "原文"],
    "created_at": ["created_at", "date", "timestamp", "发布时间", "时间", "日期"],
    "type": ["type", "post_type", "类型", "帖子类型"],
    "thread_id": ["thread_id", "conversation_id", "所属线程", "线程", "线程编号"],
    "thread_order": ["thread_order", "线程顺序"],
    "thread_total": ["thread_total", "线程总数"],
    "thread_complete": ["thread_complete", "线程是否完整"],
    "text_complete": ["text_complete", "正文是否完整", "正文或长文是否完整"],
    "contribution": ["contribution", "original_contribution", "本人新增的分析、观点、报道或创作", "原创贡献", "本人新增贡献"],
    "creation_method": ["creation_method", "创作方式"],
    "evidence": ["evidence", "证据", "证据文件名"],
    "notes": ["notes", "备注", "本人补充说明"],
    "source_url": ["source_url", "source_link", "来源链接", "来源链接或出处"],
    "source_text": ["source_text", "来源原文", "来源正文"],
    "source_owner": ["source_owner", "素材归属"],
    "media": ["media", "媒体", "媒体文件", "图片文件", "视频文件"],
}


def _value(record, key, default=""):
    for name in ALIASES.get(key, [key]):
        if name in record and record[name] is not None:
            return record[name]
    return default


def _string(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _known(value):
    text = _string(value)
    if text.casefold() in {"未知", "不确定", "不适用", "无", "none", "null", "unknown", "n/a"}:
        return ""
    return text


def _boolean(value, default=None):
    if isinstance(value, bool):
        return value
    text = _string(value).casefold()
    if text in {"是", "true", "yes", "1", "完整", "全量"}:
        return True
    if text in {"否", "false", "no", "0", "不完整"}:
        return False
    return default


def _number(value):
    try:
        result = int(value)
        return result if result >= 0 else None
    except (ValueError, TypeError):
        return None


def _type(value, raw):
    types = {"original": "original", "主帖": "original", "原创": "original", "post": "original", "tweet": "original",
             "reply": "reply", "回复": "reply", "quote": "quote", "引用帖": "quote", "引用": "quote",
             "repost": "repost", "retweet": "repost", "普通转帖": "repost", "转帖": "repost", "转发": "repost",
             "article": "article", "长文": "article"}
    label = _string(value).casefold()
    if label in types:
        return types[label]
    if raw.get("retweeted_status") or _string(raw.get("full_text", raw.get("text", ""))).startswith("RT @"):
        return "repost"
    if raw.get("in_reply_to_status_id_str") or raw.get("in_reply_to_status_id"):
        return "reply"
    if raw.get("is_quote_status") or raw.get("quoted_status_id_str") or raw.get("quoted_status_id"):
        return "quote"
    return "original"


def _owner(value):
    label = _string(value).casefold()
    return {"self": "self", "本人": "self", "本人制作": "self", "本人跨平台": "self", "other": "other",
            "他人": "other", "他人来源": "other", "third_party": "other", "mixed": "mixed", "混合素材": "mixed"}.get(label, "unknown")


def _list(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return decoded
        except json.JSONDecodeError:
            pass
    return [value]


def _normalize_media(value):
    result = []
    for item in _list(value):
        if isinstance(item, str):
            for name in re.split(r"[;；\n]", item):
                if _known(name) and name.strip() not in {"无媒体", "有媒体"}:
                    result.append({"name": name.strip(), "hash": "", "kind": "unknown"})
        elif isinstance(item, dict):
            name = _string(item.get("name", item.get("filename", item.get("media_url_https", ""))))
            digest = _string(item.get("hash", item.get("sha256", ""))).lower()
            if digest and not re.fullmatch(r"[a-f0-9]{64}", digest):
                digest = ""
            kind = item.get("kind", item.get("type", "unknown"))
            if kind not in {"image", "video", "animated_gif", "photo", "unknown"}:
                kind = "unknown"
            result.append({"name": name, "hash": digest, "kind": "image" if kind == "photo" else kind,
                           "hash_origin": item.get("hash_origin", "submitted" if digest else "")})
    return result


def _normalize_sources(record):
    result = []
    for source in _list(record.get("sources")):
        if isinstance(source, str):
            result.append({"text": "", "url": source.strip(), "owner": "unknown"})
        elif isinstance(source, dict):
            result.append({"text": _string(source.get("text", source.get("source_text", ""))),
                           "url": _string(source.get("url", source.get("link", ""))),
                           "owner": _owner(source.get("owner", source.get("ownership", "unknown"))),
                           "notes": _string(source.get("notes", ""))})
    if _known(_value(record, "source_url")) or _known(_value(record, "source_text")):
        result.append({"text": _known(_value(record, "source_text")), "url": _known(_value(record, "source_url")),
                       "owner": _owner(_value(record, "source_owner"))})
    return result


def normalize_project(value, warnings=None):
    """Normalize the public contract, preserving unknown scope as unknown."""
    warnings = warnings if warnings is not None else []
    if isinstance(value, list):
        value = {"posts": value}
    if not isinstance(value, dict):
        raise ImportErrorDetail("材料应为帖子数组或包含 posts/records/tweets 的对象。")
    records = None
    for key in ("posts", "records", "tweets"):
        if key in value:
            records = value[key]
            break
    if records is None and any(key in value for key in ("text", "full_text", "tweet")):
        records = [value]
    if not isinstance(records, list):
        raise ImportErrorDetail("未找到有效的 posts、records 或 tweets 数组。")
    if len(records) > MAX_POSTS:
        raise ImportErrorDetail(f"第一版每次最多导入 {MAX_POSTS} 条帖子，请分批整理。")
    posts, seen, invalid, text_size = [], {}, 0, 0
    for index, wrapper in enumerate(records, 1):
        if not isinstance(wrapper, dict):
            invalid += 1
            continue
        raw = wrapper.get("tweet", wrapper)
        if not isinstance(raw, dict):
            invalid += 1
            continue
        text = _known(_value(raw, "text"))
        if len(text) > MAX_TEXT_LENGTH:
            raise ImportErrorDetail(f"第 {index} 条正文超过 {MAX_TEXT_LENGTH} 字符；请拆分过长正文。")
        sources = _normalize_sources(raw)
        if any(len(s["text"]) > MAX_TEXT_LENGTH for s in sources):
            raise ImportErrorDetail(f"第 {index} 条来源正文过长。")
        text_size += len(text.encode("utf-8")) + sum(len(s["text"].encode("utf-8")) for s in sources)
        if text_size > MAX_TOTAL_TEXT:
            raise ImportErrorDetail("帖子和来源正文总量超过 5 MB，请分批整理。")
        post_id = _string(_value(raw, "id")) or f"P{index:04d}"
        url = _known(_value(raw, "url"))
        if not url and post_id.isdigit() and len(post_id) >= 8:
            url = f"https://x.com/i/status/{post_id}"
        identity = url or post_id
        if identity in seen and seen[identity] == text:
            warnings.append(f"第 {index} 条与已有记录标识及正文相同，保留首次记录并忽略重复导入；如含不同附件或来源，请先合并补充材料。")
            continue
        if identity in seen or any(p["id"] == post_id for p in posts):
            warnings.append(f"第 {index} 条标识重复但正文不同，已保留并追加编号，请核对原始材料。")
            post_id = f"{post_id}__{index}"
        seen[identity] = text
        media = _normalize_media(_value(raw, "media", []))
        entities = raw.get("extended_entities", raw.get("entities", {}))
        if not media and isinstance(entities, dict):
            media = _normalize_media(entities.get("media", []))
            for item, source_item in zip(media, entities.get("media", [])):
                if isinstance(source_item, dict):
                    media_url = source_item.get("media_url_https", source_item.get("media_url", ""))
                    item["name"] = f"{post_id}-{str(media_url).split('/')[-1]}" if media_url else item["name"]
        evidence = _list(_value(raw, "evidence", []))
        posts.append({"id": post_id, "url": url, "text": text, "created_at": _known(_value(raw, "created_at")),
                      "type": _type(_value(raw, "type"), raw), "thread_id": _known(_value(raw, "thread_id")),
                      "thread_order": _number(_value(raw, "thread_order")), "thread_total": _number(_value(raw, "thread_total")),
                      "thread_complete": _boolean(_value(raw, "thread_complete")),
                      "text_complete": _boolean(_value(raw, "text_complete")) if any(k in raw for k in ALIASES["text_complete"]) else (True if text else None),
                      "media": media, "sources": sources, "contribution": _known(_value(raw, "contribution")),
                      "creation_method": _known(_value(raw, "creation_method")), "evidence": evidence,
                      "notes": _known(_value(raw, "notes")), "missing_media": _list(raw.get("missing_media", [])),
                      "reply_to": _known(raw.get("reply_to", raw.get("in_reply_to_status_id_str", ""))),
                      "quoted_url": _known(raw.get("quoted_url", raw.get("quoted_status_permalink", "")))})
    if invalid:
        warnings.append(f"有 {invalid} 条记录不是有效帖子对象，未导入；请核对损坏或错误的记录。")
    if records and not posts:
        raise ImportErrorDetail("没有可导入的有效帖子；材料中的记录可能损坏或格式不符。")
    scope = value.get("scope", {})
    if not isinstance(scope, dict):
        scope = {}
    project = {"schema_version": 1, "account": _string(value.get("account", "")), "scope": {
        "start": _known(scope.get("start", "")), "end": _known(scope.get("end", "")),
        "timezone": _known(scope.get("timezone", "")), "complete": _boolean(scope.get("complete")),
        "total_expected": _number(scope.get("total_expected")), "selection": _known(scope.get("selection", "")),
        "known_missing": "；".join(_string(v) for v in scope.get("known_missing", []) if _string(v)) if isinstance(scope.get("known_missing"), list) else _string(scope.get("known_missing", ""))}, "posts": posts,
        "selected_candidates": [],
        "import_warnings": list(dict.fromkeys([*[_string(w) for w in _list(value.get("import_warnings", [])) if isinstance(w, str)], *warnings]))}
    candidate_lookup = {v: p["id"] for p in posts for v in (p["id"], p["url"]) if v}
    for item in _list(value.get("selected_candidates", [])):
        if isinstance(item, str) and item in candidate_lookup:
            candidate_id = candidate_lookup[item]
            if candidate_id not in project["selected_candidates"] and len(project["selected_candidates"]) < 10:
                project["selected_candidates"].append(candidate_id)
    return project


def _decode(content):
    if isinstance(content, str):
        return content.lstrip("\ufeff")
    if not isinstance(content, bytes):
        raise ImportErrorDetail("材料内容必须是文件字节或文本。")
    if len(content) > MAX_FILE_BYTES:
        raise ImportErrorDetail("文件超过 30 MB，请先筛选本次相关帖子或分批导入。")
    try:
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            return content.decode("utf-16")
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportErrorDetail("文件不是 UTF-8/UTF-16 文本；请以 UTF-8（可含 BOM）重新保存。") from exc


def _json(text, archive=False):
    if archive:
        match = re.fullmatch(r"\s*window\.YTD\.(?:tweets?|tweets?_part\d+)\.part\d+\s*=\s*([\s\S]+?)\s*;?\s*", text)
        if not match:
            raise ImportErrorDetail("仅支持 X 公开帖子归档赋值（window.YTD.tweets.partN = JSON）；不会执行 JavaScript。")
        text = match.group(1).rstrip().rstrip(";").rstrip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ImportErrorDetail("JSON 内容损坏、嵌套过深或含有额外代码；未导入。") from exc


def _csv(text):
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
        if not reader.fieldnames:
            raise ImportErrorDetail("CSV 缺少字段标题行。")
        recognized = {name for names in ALIASES.values() for name in names}
        if not any(name.strip() in recognized for name in reader.fieldnames):
            raise ImportErrorDetail("CSV 标题未包含帖子字段；至少需要 text/正文 或 url/帖子链接。")
        records = []
        for line, record in enumerate(reader, 2):
            if None in record:
                raise ImportErrorDetail(f"CSV 第 {line} 行列数超过标题列，请检查引号和分隔符。")
            records.append({str(k).strip(): v for k, v in record.items()})
            if len(records) > MAX_POSTS:
                raise ImportErrorDetail(f"第一版每次最多导入 {MAX_POSTS} 条帖子。")
        return {"posts": records}
    except csv.Error as exc:
        raise ImportErrorDetail("CSV 格式损坏，请检查引号和分隔符。") from exc


def _fields(text):
    result = {}
    for match in re.finditer(r"(?m)^[ \t]*-[ \t]*([^：:\n]+)[：:][ \t]*([^\n]*)$", text):
        result[match.group(1).strip()] = match.group(2).strip()
    return result


def _section_body(text, heading):
    match = re.search(r"\*\*" + re.escape(heading) + r"[：:]?\*\*\s*([\s\S]*?)(?=\n\*\*|\n#{1,6}\s|\Z)", text)
    return match.group(1).strip() if match else ""


def _markdown(text, warnings):
    basic = _fields(text.split("## 二、", 1)[0])
    scope = {"timezone": basic.get("时区（账号评估必填；仅样本初评可填“不适用”）", basic.get("时区", "")),
             "complete": basic.get("是否包含该期间全部本人发帖", ""),
             "total_expected": basic.get("已知该期间帖子总数", ""),
             "selection": basic.get("选择方式", ""), "known_missing": basic.get("缺失详情", "")}
    scope["known_missing"] = "；".join(v for v in [basic.get("已知缺失", ""), scope["known_missing"]] if _known(v))
    date_line = next((v for k, v in basic.items() if k.startswith("时间范围")), "")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", date_line)
    if len(dates) >= 2:
        scope["start"], scope["end"] = dates[:2]
    account = basic.get("账号链接或 @用户名", basic.get("账号", ""))
    posts = []
    segments = list(re.finditer(r"(?m)^#{2,6}\s+(P\d+)\s*$", text))
    for index, marker in enumerate(segments):
        end = segments[index + 1].start() if index + 1 < len(segments) else len(text)
        section = text[marker.end():end].split("## 四、", 1)[0]
        fields = _fields(section)
        post_text = _section_body(section, "帖子原文")
        if post_text.startswith("在这里粘贴完整原文"):
            post_text = ""
        record = {"id": _known(fields.get("帖子标识（如能取得）")) or marker.group(1), "material_id": marker.group(1),
                  "url": fields.get("帖子链接", ""), "text": post_text,
                  "created_at": fields.get("发布时间（含时区；不知道则填未知）", fields.get("发布时间", "")),
                  "type": fields.get("类型", ""), "text_complete": fields.get("正文或长文是否完整", ""),
                  "media": [], "notes": _section_body(section, "本人补充说明（可选）")}
        thread = fields.get("所属线程及顺序", "")
        match = re.search(r"(.+?)[，,]\s*第\s*(\d+)\s*条[，,]\s*共\s*(\d+)\s*条", thread)
        if match:
            record.update(thread_id=match.group(1).strip(), thread_order=match.group(2), thread_total=match.group(3))
        elif _known(thread):
            record["thread_id"] = thread
        for label, kind in [("图片文件", "image"), ("视频或 GIF 文件", "video")]:
            for name in re.split(r"[;；、\n]", _known(fields.get(label, ""))):
                if name.strip():
                    record["media"].append({"name": name.strip(), "kind": kind})
        if _known(fields.get("无法提供的媒体及原因", "")):
            record["missing_media"] = [fields["无法提供的媒体及原因"]]
        if any(_known(record.get(k)) for k in ("url", "text", "created_at")) or record["media"]:
            posts.append(record)
    source_segments = list(re.finditer(r"(?m)^#{2,6}\s+来源记录\s+S\d+\s*$", text))
    for index, marker in enumerate(source_segments):
        end = source_segments[index + 1].start() if index + 1 < len(source_segments) else len(text)
        fields = _fields(text[marker.end():end].split("## 五、", 1)[0])
        target = _known(fields.get("对应帖子链接或编号", ""))
        if not target:
            continue
        matched = [p for p in posts if target in {p["id"], p.get("material_id"), p["url"]}]
        if not matched:
            warnings.append(f"来源记录对应的帖子 {target} 未找到，未自动关联。")
            continue
        for post in matched:
            source_text = _section_body(text[marker.end():end].split("## 五、", 1)[0], "来源原文（用于文字对比，可选）") or fields.get("来源原文", "")
            if source_text.startswith("在这里"):
                source_text = ""
            source_url = _known(fields.get("来源链接或出处", "")) or _known(fields.get("本人在其他平台发布的链接（适用时填写）", ""))
            post.setdefault("sources", []).append({"url": source_url, "text": source_text, "owner": fields.get("素材归属", "unknown")})
            post["contribution"] = "；".join(v for v in [post.get("contribution", ""), _known(fields.get("本人新增的分析、观点、报道或创作", ""))] if v)
            post["creation_method"] = fields.get("创作方式", "")
            post["evidence"] = [_known(fields.get("证据文件名", ""))] if _known(fields.get("证据文件名", "")) else []
    if not posts:
        raise ImportErrorDetail("Markdown 未包含已填写的 P001 等帖子记录；空白模板不能用于检测。")
    selected = []
    candidates = text.split("## 二、申请候选", 1)
    if len(candidates) == 2:
        selected = re.findall(r"https?://(?:www\.)?(?:x|twitter)\.com/[^\s　]+/status/\d+", candidates[1].split("## 三、", 1)[0])
    return {"account": account, "scope": scope, "posts": posts, "selected_candidates": selected}


def _safe_member(name):
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", name)


def _public_file(name):
    if _private_member(name):
        return False
    base = PurePosixPath(name.replace("\\", "/")).name.casefold()
    if re.fullmatch(r"tweets?(?:[-_]part\d+)?\.(?:js|json|csv)", base):
        return True
    return base in {"posts.json", "records.json", "project.json", "posts.csv", "提交表.md", "x原创检测提交表.md", "x原创检测提交表模板.md"}


def _private_member(name):
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return any(re.search(r"direct[-_ ]?messages?|(?:^|[-_])dm(?:[-_]|$)|contacts?|address[-_ ]?book|account|私信|通讯录", part.casefold()) for part in parts)


def _zip(content, warnings):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_ENTRIES:
                raise ImportErrorDetail("ZIP 条目超过 5000 个；请只保留本次相关帖子和媒体。")
            if any(not _safe_member(m.filename) for m in members):
                raise ImportErrorDetail("ZIP 包含绝对路径或上级目录路径，已拒绝；未向磁盘解压。")
            selected = [m for m in members if not m.is_dir() and _public_file(m.filename)]
            if not selected:
                raise ImportErrorDetail("ZIP 未包含支持的公开帖子文件（tweets.js/posts.json/posts.csv/提交表.md）；其他账户文件不会读取。")
            if sum(m.file_size for m in selected) > MAX_ZIP_EXPANDED_BYTES:
                raise ImportErrorDetail("ZIP 中选中的帖子文件展开超过 100 MB，已拒绝。")
            projects, imported_files = [], []
            for member in selected:
                if member.flag_bits & 1:
                    raise ImportErrorDetail("ZIP 中的帖子文件已加密，请提供未加密的筛选文件。")
                try:
                    child = import_data(archive.read(member), member.filename)
                except (ImportErrorDetail, RuntimeError, zipfile.BadZipFile) as exc:
                    raise ImportErrorDetail(f"ZIP 内帖子文件 {member.filename} 导入失败：{exc}") from exc
                warnings.extend(child["warnings"])
                projects.append(child["project"])
                imported_files.append(member.filename)
            value = dict(projects[0])
            value["posts"] = [post for project in projects for post in project["posts"]]
            project = normalize_project(value, warnings)
            names = {m.filename.replace("\\", "/"): m for m in members if not m.is_dir() and not _private_member(m.filename)}
            matched_count, media_bytes = 0, 0
            for post in project["posts"]:
                for media in post["media"]:
                    wanted = media["name"].replace("\\", "/")
                    matches = [m for path, m in names.items() if path == wanted or PurePosixPath(path).name == PurePosixPath(wanted).name]
                    if len(matches) != 1:
                        if len(matches) > 1:
                            warnings.append(f"媒体文件 {media['name']} 有多个同名文件，未自动关联。")
                        continue
                    member = matches[0]
                    ext = PurePosixPath(member.filename).suffix.casefold()
                    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm", ".m4v"}:
                        continue
                    media_bytes += member.file_size
                    if member.file_size > MAX_FILE_BYTES or media_bytes > MAX_ZIP_EXPANDED_BYTES:
                        warnings.append(f"媒体文件 {media['name']} 超过本地处理上限，未计算哈希。")
                        continue
                    digest = hashlib.sha256(archive.read(member)).hexdigest()
                    media.update(hash=digest, hash_origin="local_file")
                    matched_count += 1
            skipped = len([m for m in members if not m.is_dir()]) - len(selected)
            warnings.append(f"ZIP 仅读取 {len(selected)} 个受支持的公开帖子文件和 {matched_count} 个明确关联的媒体；其余 {skipped} 个条目未作为帖子导入，未读取私信或账户资料，未解压到磁盘。")
            project["import_warnings"] = list(dict.fromkeys(warnings))
            return project, imported_files
    except (zipfile.BadZipFile, OSError, NotImplementedError) as exc:
        raise ImportErrorDetail("ZIP 损坏或使用不支持的压缩方式，未导入。") from exc


def import_data(content, filename="posts.json"):
    warnings = []
    if isinstance(content, bytes) and len(content) > MAX_FILE_BYTES:
        raise ImportErrorDetail("文件超过 30 MB，请先筛选本次相关帖子或分批导入。")
    ext = PurePosixPath(str(filename).replace("\\", "/")).suffix.casefold()
    if ext == ".zip":
        if not isinstance(content, bytes):
            raise ImportErrorDetail("ZIP 必须以二进制文件导入。")
        project, imported_files = _zip(content, warnings)
    else:
        text = _decode(content)
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ImportErrorDetail("文本文件超过 30 MB。")
        if ext == ".js":
            value = _json(text, archive=True)
        elif ext in {".json", ""}:
            value = _json(text)
        elif ext in {".csv", ".tsv"}:
            value = _csv(text)
        elif ext in {".md", ".markdown"}:
            value = _markdown(text, warnings)
        else:
            raise ImportErrorDetail("不支持此格式；请选择 JSON、CSV、X 公开帖子归档 JS、填写后的 Markdown 提交表或筛选 ZIP。")
        project = normalize_project(value, warnings)
        imported_files = [str(filename)]
    return {"project": project, "warnings": list(dict.fromkeys(warnings)), "imported_files": imported_files}


import_file = import_data
