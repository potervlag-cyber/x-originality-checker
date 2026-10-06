"""Explainable offline checks of supplied material, not X's private classifier."""
from __future__ import annotations

import datetime as dt
import difflib
import email.utils
import re
import unicodedata
from collections import Counter, defaultdict

from importers import normalize_project

VERSION = "0.1.0"
STATUS_LABELS = {"high_risk": "高风险材料", "review": "需人工复核", "insufficient": "材料不足", "low_signal": "未发现明显文本风险"}
LIMITATIONS = [
    "本工具是离线材料初评，未接入 X 的内部审核；与官方结果的一致性尚未验证，不提供通过概率。",
    "仅比较本次提供的帖子与来源正文，未访问帖子/来源链接，未执行全网查重。未发现重复不能证明原创。",
    "媒体只核对文件信息与 SHA-256 完全相同的文件；未检查视觉相似、视频内容或媒体创作归属。",
    "草稿、证据文件名、作者自述和素材授权记录仅作为待核实的线索，不等于已验证作者身份或原创资格。",
    "推荐最多 10 条初评候选；官方对 10 篇样本的审核权重未见公开说明。回复和普通转帖默认不列入推荐，这是本工具的候选策略。",
    "日期范围与内容完整性依赖提交材料；本工具不判断会员、展示量、认证粉丝或其他收益资格。",
]


def _clean(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = re.sub(r"https?://\S+", "", text)
    return "".join(c for c in text if c.isalnum())


def _grams(text, width=4):
    if len(text) < width:
        return {text} if text else set()
    return {text[i:i + width] for i in range(len(text) - width + 1)}


def _similarity(left, right, containment=False):
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    a, b = _grams(left), _grams(right)
    intersection = len(a & b)
    jaccard = intersection / max(len(a | b), 1)
    if containment:
        return intersection / max(len(a), 1)
    length_ratio = min(len(left), len(right)) / max(len(left), len(right))
    if jaccard < 0.4 or length_ratio < 0.7:
        return jaccard
    if max(len(left), len(right)) <= 2500:
        return max(jaccard, difflib.SequenceMatcher(None, left, right, autojunk=False).ratio())
    return jaccard


def _timezone(label):
    label = str(label or "Asia/Shanghai").strip()
    if label in {"Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "北京时间", "UTC+8", "UTC+08:00"} or "Asia/Shanghai" in label:
        return dt.timezone(dt.timedelta(hours=8))
    if label in {"UTC", "Etc/UTC", "Z", "UTC+0", "UTC+00:00"}:
        return dt.timezone.utc
    match = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", label)
    if match:
        hours, minutes = int(match[2]), int(match[3] or 0)
        if hours > 14 or minutes >= 60 or (hours == 14 and minutes):
            raise ValueError("时区偏移不正确，请填写 UTC、Asia/Shanghai 或 UTC±HH:MM。")
        return dt.timezone((1 if match[1] == "+" else -1) * dt.timedelta(hours=hours, minutes=minutes))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(label)
    except Exception as exc:
        raise ValueError("此时区无法解析；第一版可使用 Asia/Shanghai、UTC 或 UTC±HH:MM。") from exc


def _date(value, timezone):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone)
    return moment.astimezone(timezone).date()


def _window(project):
    scope = project["scope"]
    zone = _timezone(scope["timezone"])
    limits = []
    for key in ("start", "end"):
        value = scope[key]
        if not value:
            limits.append(None)
        else:
            try:
                limits.append(dt.date.fromisoformat(value))
            except ValueError as exc:
                raise ValueError("评估起止日期应为 YYYY-MM-DD。") from exc
    start, end = limits
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    selected, outside, invalid = [], [], []
    for post in project["posts"]:
        date = _date(post["created_at"], zone)
        if (start or end) and date is None:
            invalid.append(post["id"])
        elif date and ((start and date < start) or (end and date > end)):
            outside.append(post["id"])
        else:
            selected.append(post)
    return selected, outside, invalid, zone


def _reason(code, message, evidence=None):
    return {"code": code, "message": message, "evidence": evidence or {}}


def analyze(project):
    """Return auditable, mutually exclusive material statuses and at most ten candidates."""
    project = normalize_project(project)
    posts, outside, invalid_dates, zone = _window(project)
    checks = {p["id"]: [] for p in posts}
    statuses = {p["id"]: set() for p in posts}

    def add(post_id, level, code, message, evidence=None):
        checks[post_id].append(_reason(code, message, evidence))
        if level:
            statuses[post_id].add(level)

    clean = {p["id"]: _clean(p["text"]) for p in posts}
    exact = defaultdict(list)
    for post in posts:
        if len(clean[post["id"]]) >= 24 and post["type"] != "repost":
            exact[clean[post["id"]]].append(post["id"])
    duplicates = set()
    for ids in exact.values():
        if len(ids) < 2:
            continue
        for post_id in ids:
            other = [i for i in ids if i != post_id]
            add(post_id, "high_risk", "internal_exact", "与本次账号材料中的其他帖子正文完全或仅标点/链接不同；需核对重复发布原因。", {"related_posts": other})
            duplicates.add(post_id)

    # An inverted 4-character index avoids unbounded all-pairs difflib comparisons.
    gram_index = defaultdict(list)
    gram_sets = {}
    comparison_limited = False
    for post in posts:
        post_id, text = post["id"], clean[post["id"]]
        if len(text) < 40 or post["type"] == "repost":
            continue
        grams = _grams(text)
        gram_sets[post_id] = grams
        counts = Counter()
        for gram in grams:
            prior = gram_index[gram]
            if len(prior) <= 150:
                counts.update(prior)
        possible = [prior for prior, count in counts.most_common(120)
                    if count / max(min(len(grams), len(gram_sets[prior])), 1) >= 0.5]
        if len(counts) > 120:
            comparison_limited = True
        for prior in possible:
            if clean[prior] == text:
                continue
            similarity = _similarity(text, clean[prior])
            if similarity >= 0.84:
                for target, other in ((post_id, prior), (prior, post_id)):
                    add(target, "review", "internal_near", "与账号材料中的其他帖子高度相似；改写、重复模板或再发布原因需人工复核。", {"related_posts": [other], "comparison_similarity": round(similarity, 3)})
                    duplicates.add(target)
        for gram in grams:
            if len(gram_index[gram]) <= 150:
                gram_index[gram].append(post_id)

    media_index = defaultdict(list)
    for post in posts:
        for media in post["media"]:
            if media.get("hash"):
                media_index[media["hash"]].append((post["id"], media["name"]))
    for digest, entries in media_index.items():
        ids = sorted({entry[0] for entry in entries})
        if len(ids) >= 2:
            for post_id in ids:
                add(post_id, "review", "media_same_hash", "与其他帖子提供的媒体具有相同 SHA-256；重复使用、本人再发布或他人来源需核实，不能据此认定搬运。", {"related_posts": [i for i in ids if i != post_id], "hash": digest})

    threads = defaultdict(list)
    for post in posts:
        if post["thread_id"]:
            threads[post["thread_id"]].append(post)
    for thread_id, members in threads.items():
        totals = {p["thread_total"] for p in members if p["thread_total"]}
        orders = [p["thread_order"] for p in members if p["thread_order"]]
        missing = max(totals, default=len(members)) > len(set(orders) if orders else members)
        explicit_missing = any(p["thread_complete"] is False for p in members)
        inconsistent = len(totals) > 1 or len(orders) != len(set(orders)) or bool(totals and orders and max(orders) > max(totals))
        unknown = not totals and not all(p["thread_complete"] is True for p in members)
        if missing or explicit_missing or inconsistent or unknown:
            for post in members:
                message = "线程分段缺失或顺序/总数不一致，需补全后评估。" if missing or explicit_missing or inconsistent else "提供了线程关系，但尚未确认线程是否完整。"
                add(post["id"], "insufficient", "thread_incomplete", message, {"thread_id": thread_id, "provided": len(members), "declared_totals": sorted(totals), "orders": orders})

    phrases = ["点赞转发关注", "关注并转发", "转发并关注", "点赞加关注", "互关互赞", "互赞互转", "likeandrepost", "followandrepost", "followandretweet"]
    engagement = defaultdict(list)
    for post in posts:
        normalized = clean[post["id"]]
        for phrase in phrases:
            if phrase in normalized:
                engagement[phrase].append(post["id"])
    for phrase, ids in engagement.items():
        if len(set(ids)) >= 3:
            for post_id in set(ids):
                add(post_id, "review", "repeated_engagement_phrase", "同一明确互动请求在至少 3 条帖子中反复出现；请复核上下文与账号行为，此提示不认定原创违规。", {"phrase": phrase, "occurrences": len(set(ids))})

    source_comparisons = 0
    source_urls_only = 0
    hashed_media = 0
    for post in posts:
        post_id, normalized = post["id"], clean[post["id"]]
        if post["type"] == "repost":
            add(post_id, "review", "repost_record", "普通转帖用于记录账号发布构成，不列为本人原创申请候选；转帖本身不等于账号违规。")
        elif not normalized:
            add(post_id, "insufficient", "missing_text", "未提供可检查的帖子正文；链接未访问，媒体或截图未作内容识别。")
        if post["text_complete"] is False or (post["text_complete"] is None and post["type"] != "repost"):
            add(post_id, "insufficient", "text_incomplete", "正文完整性尚未确认或明确缺失；请补充完整主帖/长文。")
        if post["missing_media"]:
            add(post_id, "insufficient", "media_missing", "存在未提供的媒体，无法评价帖子的完整内容。", {"missing": post["missing_media"]})
        for media in post["media"]:
            if media["hash"]:
                hashed_media += 1
            else:
                add(post_id, "insufficient", "media_reference_only", "媒体只提供了名称或链接，未核对文件内容；请关联实际媒体文件。", {"name": media["name"]})
        if post["media"] and all(m["hash"] for m in post["media"]):
            add(post_id, "review", "media_content_unchecked", "已取得媒体哈希，可核对完全相同文件；媒体画面、视频内容与原创归属仍需人工复核。")
        meaningful = len(_clean(post["contribution"])) >= 35
        for source in post["sources"]:
            source_clean = _clean(source["text"])
            if not source_clean:
                if source["url"]:
                    source_urls_only += 1
                    add(post_id, "review", "source_link_unchecked", "只提供了来源链接，未获取来源正文或访问链接；无法完成来源比对。", {"source_url": source["url"]})
                else:
                    add(post_id, "review", "source_missing", "来源记录缺少可比对正文和有效出处，请补充。")
                continue
            source_comparisons += 1
            if len(normalized) < 24 or len(source_clean) < 24:
                add(post_id, None, "source_short", "帖子或来源正文较短，不据短语重合判断搬运。")
                continue
            overlap = _similarity(normalized, source_clean, containment=True)
            evidence = {"source_url": source["url"], "source_owner": source["owner"], "supplied_text_overlap": round(overlap, 3)}
            if overlap >= 0.88:
                if source["owner"] == "self":
                    add(post_id, "review", "self_crosspost_claim", "正文与所提供来源高度重合，但声明为本人作品/跨平台发布；需核实作者归属，不能直接认定搬运。", evidence)
                elif meaningful:
                    add(post_id, "review", "source_overlap_with_contribution", "正文与提供来源高度重合，并有新增贡献说明；需核实新增价值是否实际体现在帖子中。", evidence)
                else:
                    add(post_id, "high_risk", "source_overlap_no_contribution", "正文与提供来源高度重合，且缺少具体新增贡献说明；疑似转载或轻微改写，需复核来源与创作过程。", evidence)
            elif overlap >= 0.5:
                add(post_id, "review", "source_partial_overlap", "与提供的来源存在较多文字重合；引用范围及新增贡献需人工复核。", evidence)
            else:
                add(post_id, None, "source_no_high_overlap", "与本次提供的来源正文未发现高度文字重合；不等于已证明原创。", evidence)
                if source["owner"] != "self" and not meaningful:
                    add(post_id, "review", "source_contribution_unexplained", "已提供他人或归属不明的来源，但尚未具体说明本人的新增贡献；请补充并核实帖子中的实际贡献。")
        if post["type"] == "quote" and not post["sources"]:
            add(post_id, "review", "quote_source_missing", "引用帖尚未提供被引用内容和来源关系，需补充后判断新增贡献。")
        if post["creation_method"]:
            add(post_id, None, "creation_method_declared", "创作方式为作者提供的背景信息；工具辅助或自动生成标签不直接决定原创资格。", {"creation_method": post["creation_method"]})
        if post["evidence"]:
            add(post_id, None, "evidence_reference", "已登记创作证据线索，证据内容与作者身份尚未核验。", {"references": post["evidence"]})

    thread_entries = {thread_id: min(members, key=lambda p: p["thread_order"] if p["thread_order"] is not None else float("inf"))["id"] for thread_id, members in threads.items()}
    results, findings, pool = [], [], []
    for post in posts:
        post_id = post["id"]
        levels = statuses[post_id]
        status = next((s for s in ("high_risk", "insufficient", "review") if s in levels), "low_signal")
        if not checks[post_id]:
            checks[post_id].append(_reason("no_supplied_text_match", "在本次提供材料的文字比较范围内未发现明显重复风险；未执行全网查重。"))
        reasons = checks[post_id]
        if post["type"] in {"reply", "repost"}:
            candidate_reason = "本工具默认不推荐回复或普通转帖；此筛选策略不代表官方原创定义。"
        elif status != "low_signal":
            candidate_reason = "存在风险、复核事项或缺失材料，暂不列为申请推荐。"
        elif len(clean[post_id]) < 40:
            candidate_reason = "正文较短，初评材料不足以支持推荐；不表示短帖必然不原创。"
        else:
            candidate_reason = "正文完整且在本次文字比对中未发现明显风险，可作为待人工确认的申请候选。"
        eligible = post["type"] in {"original", "article", "quote"} and status == "low_signal" and len(clean[post_id]) >= 40
        if post["thread_id"] and thread_entries[post["thread_id"]] != post_id:
            eligible = False
            candidate_reason = "同一线程只推荐最早顺序的入口记录；请保留全体分段，官方计数方式仍需以申请页面为准。"
        item = {**post, "status": status, "status_label": STATUS_LABELS[status], "reasons": reasons,
                "candidate_eligible": eligible, "candidate_reason": candidate_reason}
        results.append(item)
        findings.extend({"post_id": post_id, **reason} for reason in reasons)
        if eligible:
            # This is sorting support, never a numerical official score/probability.
            support = (bool(post["contribution"]), bool(post["evidence"]), bool(post["sources"]), min(len(clean[post_id]), 2000))
            pool.append((support, item))
    pool.sort(key=lambda pair: pair[0], reverse=True)
    candidates, candidate_texts = [], set()
    for _, item in pool:
        normalized = clean[item["id"]]
        if normalized in candidate_texts:
            continue
        candidate_texts.add(normalized)
        candidates.append({"id": item["id"], "url": item["url"], "text": item["text"], "status_label": item["status_label"],
                           "reasons": item["reasons"], "candidate_reason": item["candidate_reason"], "rank": len(candidates) + 1})
        if len(candidates) >= 10:
            break
    actual_dates = sorted(d for p in posts if (d := _date(p["created_at"], zone)))
    counts = {key: sum(p["status"] == key for p in results) for key in STATUS_LABELS}
    scope = project["scope"]
    expected = scope["total_expected"]
    percent = round(len(posts) / expected * 100, 1) if expected and len(posts) <= expected and not invalid_dates else None
    coverage_notes = []
    if invalid_dates:
        coverage_notes.append(f"{len(invalid_dates)} 条时间未知/无法解析，未纳入指定日期窗口的检查。")
    if outside:
        coverage_notes.append(f"{len(outside)} 条位于指定日期窗口之外，未纳入本次检查。")
    if expected is not None and len(posts) > expected:
        coverage_notes.append("提供数量超过声明的期间总帖数，总数或日期范围需核对；不计算覆盖率。")
    if scope["complete"] is not True:
        coverage_notes.append("未声明提供全部期间帖子；报告只覆盖已提供且纳入窗口的记录。")
    if scope["known_missing"]:
        coverage_notes.append("作者声明的缺失：" + scope["known_missing"])
    if comparison_limited:
        coverage_notes.append("大量相似文本触发近似比对预算限制；已执行完全重复分组，近似结果可能不完整。")
    coverage = {"imported_posts": len(project["posts"]), "analyzed_posts": len(posts),
                "outside_scope": outside, "invalid_date": invalid_dates,
                "provided_text": sum(bool(clean[p["id"]]) for p in posts),
                "complete_text": sum(bool(clean[p["id"]]) and p["text_complete"] is True for p in posts),
                "media_references": sum(len(p["media"]) for p in posts), "hashed_media": hashed_media,
                "source_text_comparisons": source_comparisons, "source_links_unchecked": source_urls_only,
                "links_visited": 0, "expected_posts": expected, "coverage_percent": percent,
                "coverage_basis": "相对使用者声明总数的材料数量比例，不是官方检测覆盖率。" if percent is not None else "总数未知、材料矛盾或时间缺失，未计算精确覆盖率。",
                "actual_start": actual_dates[0].isoformat() if actual_dates else "",
                "actual_end": actual_dates[-1].isoformat() if actual_dates else "", "scope": scope,
                "notes": coverage_notes, "approximate_comparison_limited": comparison_limited}
    filtered_project = {**project, "posts": posts}
    return {"version": VERSION, "summary": {"total": len(posts), "counts": counts,
            "distinct_candidates": len(candidates), "candidate_pool": len(pool),
            "account_conclusion": "没有可评估的帖子材料，请补充正文并核对日期范围。" if not posts else ("发现需处理或复核的材料" if counts["high_risk"] or counts["review"] or counts["insufficient"] else "本次文字材料未发现明显风险；尚不能证明账号符合官方原创要求。")},
            "posts": results, "findings": findings, "candidates": candidates, "coverage": coverage,
            "limitations": LIMITATIONS, "filtered_project": filtered_project}
