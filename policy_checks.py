"""Evidence-limited screening against a saved X policy reference.

These are tool risk signals, never official decisions or a complete policy list.
The reference is the project's previously verified 2026-10-06 notes. Attempts to
read the official page on 2026-10-07 returned 403, so it is not called live-verified.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit


POLICY_URL = "https://help.x.com/en/using-x/original-content-rewards"
SOURCE = {
    "url": POLICY_URL,
    "reference_date": "2026-10-06",
    "live_verification": "unavailable",
    "live_attempt_date": "2026-10-07",
    "source_kind": "previously_verified_project_notes",
    "previous_verification": "2026-10-06（项目已保存的官方页面核查记录）",
    "note": "采用项目于 2026-10-06 已核查页面的保存释义；2026-10-07 重新访问返回 403，现行页面未成功复核。下面列出已保存的部分要求，不是官方完整清单或逐字原文。",
}
# Four representative records keep the compact archive report bounded. Total
# evidence counts remain exact, so large duplicate groups do not disappear.
MAX_EVIDENCE = 4
MAX_DETAIL = 360


class _Evidence(list):
    """Keep report samples bounded while counting every evidence record."""

    def __init__(self):
        super().__init__()
        self.total = 0

    def append(self, value):
        self.total += 1
        if len(self) < MAX_EVIDENCE:
            super().append(value)


AUTOMATION = re.compile(
    r"自动(?:化)?(?:生成|创作|创建|发布|发帖)|(?:AI|人工智能)(?:自动)?(?:生成|创作)|"
    r"(?:fully[ -]?)?(?:ai[ -]?generated|automatically[ -]?(?:generated|posted)|automated[ -]?(?:creation|publishing|posting))",
    re.I,
)
HUMAN_METHOD = re.compile(r"本人(?:创作|撰写)|人工(?:创作|撰写|发布)|human[ -]?(?:written|authored)", re.I)
MONETIZATION = re.compile(r"变现|收益|拿工资|创作者收入|monetiz\w*|revenue|earnings", re.I)
MONETIZATION_GUIDANCE = re.compile(
    r"如何.{0,24}(?:变现|收益|拿工资)|怎么.{0,24}(?:变现|收益|拿工资)|"
    r"(?:提升|提高|最大化|优化|冲|薅).{0,18}(?:收益|收入|工资)|"
    r"变现(?:教学|教程|技巧)|(?:收益|收入)(?:教学|教程|最大化|技巧)|"
    r"(?:how to|maximi[sz]e|increase|boost).{0,35}(?:monetiz\w*|revenue|earnings)",
    re.I,
)


def _clean(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(c for c in re.sub(r"https?://\S+", "", text) if c.isalnum())


def _full_text(post):
    return post.get("text_complete") is True and len(_clean(post.get("text"))) >= 24


def _automation_declared(method):
    for match in AUTOMATION.finditer(method):
        prefix = method[max(0, match.start() - 18):match.start()]
        if not re.search(r"(?:不是|并非|没有|不用|禁止|避免|不使用|未使用|非|未|不|not|never|no)[\s:：]*$", prefix, re.I):
            return True
    return False


def _reason_map(assessed):
    if isinstance(assessed, dict):
        assessed = assessed.get("posts", [])
    return {str(p.get("id", "")): p for p in (assessed or []) if isinstance(p, dict)}


def _reasons(post):
    return {str(r.get("code")): r for r in post.get("reasons", []) if isinstance(r, dict)}


def _evidence(post, code, detail, **extra):
    return {"post_id": str(post.get("id", "")), "url": str(post.get("url", "")),
            "code": code, "detail": str(detail)[:MAX_DETAIL], **extra}


def _item(identifier, title, requirement, posts, checked, signals, evidence, interpretation, *, strength="low", auxiliary=False):
    count, checked_count = len(signals), len(checked)
    denominator = len(posts)
    if count:
        status = "signals_found"
        degree = {"high": "高（需复核）", "medium": "中（需复核）", "low": "低（仅弱线索）"}[strength]
    elif checked_count:
        status, degree = "no_detected_signal", "未发现风险线索"
    else:
        status, degree = "unknown", "证据不足"
    return {"id": identifier, "title": title, "official_requirement": requirement,
            "scope": "auxiliary" if auxiliary else "content", "status": status,
            "degree": degree, "degree_basis": "工具风险线索强度；不是 X 官方认定的不符合程度。",
            "numerator": count, "denominator": denominator, "signal_count": count,
            "signal_percent": round(count / denominator * 100, 1) if denominator and (count or checked_count) else None,
            "assessed_count": checked_count, "unknown_count": denominator - checked_count,
            "interpretation": interpretation, "evidence": evidence[:MAX_EVIDENCE],
            "evidence_total": getattr(evidence, "total", len(evidence)), "official_score": None, "official_noncompliance_percent": None}


def _same_post(post, match):
    if match.get("source_kind") == "same_post":
        return True
    try:
        parts = urlsplit(str(match.get("url", "")))
    except ValueError:
        return False
    if (parts.hostname or "").casefold() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}:
        return False
    found = re.search(r"/status/(\d+)(?:/|$)", parts.path)
    return bool(found and found[1] == str(post.get("id", "")))


def _web_posts(web_check):
    if not isinstance(web_check, dict):
        return {}
    return {str(p.get("id", "")): p for p in web_check.get("posts", []) if isinstance(p, dict)}


def _web_text_complete(entry):
    if entry.get("text_truncated") is True:
        return False
    original = entry.get("original_chars")
    checked = entry.get("checked_chars")
    if (isinstance(original, int) and not isinstance(original, bool)
            and isinstance(checked, int) and not isinstance(checked, bool)
            and original >= 0 and checked >= 0):
        return checked >= original
    # Older supplied fixtures have no character counts. Do not infer a missing
    # field is zero, and do not change their previously assessed coverage.
    return True


def _match_score(match):
    score = match.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    # Backend's contract uses 0..1. Unknown/invalid scales never become evidence.
    return float(score) if 0 <= score <= 1 else None


def assess_policy(posts, assessed_posts=None, web_check=None):
    """Return per-requirement signals; unknown evidence never becomes compliance.

    ``posts`` contains normalized project records. ``assessed_posts`` is the
    existing analyzer's post list (or result dict). ``web_check`` uses the search
    backend's aggregate ``posts`` list. This function does not perform I/O.
    """
    own = [p for p in (posts or []) if isinstance(p, dict) and p.get("type") != "repost"]
    assessed, web = _reason_map(assessed_posts), _web_posts(web_check)
    original_checked, original_signals, original_evidence = set(), set(), _Evidence()
    automation_checked, automation_signals, automation_evidence = set(), set(), _Evidence()
    topic_checked, topic_signals, topic_evidence = set(), set(), _Evidence()
    interaction_checked, interaction_signals, interaction_evidence = set(), set(), _Evidence()
    ip_evidence = _Evidence()
    original_strength = "low"

    for post in own:
        pid = str(post.get("id", ""))
        reasons = _reasons(assessed.get(pid, post))
        complete = _full_text(post)
        content_checked = complete and not post.get("media") and not post.get("missing_media")
        source_codes = {"source_overlap_no_contribution", "source_overlap_with_contribution", "source_partial_overlap", "source_no_high_overlap", "self_crosspost_claim"}
        if content_checked and source_codes.intersection(reasons):
            original_checked.add(pid)
        for code in sorted(source_codes.intersection(reasons)):
            reason = reasons[code]
            if code == "source_no_high_overlap":
                continue
            original_signals.add(pid)
            if code == "source_overlap_no_contribution":
                # The source owner itself is submitted information, not identity verification.
                original_strength = "medium"
            original_evidence.append(_evidence(post, code, reason.get("message", "与已提供来源重合，新增贡献及归属需复核。"),
                source_evidence=reason.get("evidence", {})))
        for code in ("internal_exact", "internal_near"):
            if code in reasons:
                original_evidence.append(_evidence(post, code, "账号内重复仅说明材料多样性有限；本人再次发布不等于违反原创要求，不计为外部来源风险。"))

        entry = web.get(pid, {})
        if (content_checked and entry.get("status") in {"matched", "no_match"}
                and entry.get("sources_checked", 0) and _web_text_complete(entry)):
            original_checked.add(pid)
        for match in entry.get("matches", []):
            if not isinstance(match, dict) or _same_post(post, match):
                continue
            score = _match_score(match)
            if score is None or score < 0.5 or match.get("temporal_relation") == "later":
                continue
            original_signals.add(pid)
            is_snippet = match.get("source_kind") in {"snippet", "search_snippet"} or match.get("page_status") not in {"fetched", "ok", "success", "body_fetched"}
            if score >= 0.88 and not is_snippet:
                original_strength = "medium"
            original_evidence.append(_evidence(post, "web_text_overlap", "公开来源存在文字重合；" + ("仅搜索摘要可用，" if is_snippet else "") + "作者归属、引用、新增贡献与传播方向需人工复核，不能直接认定抄袭。",
                source_url=str(match.get("url", "")), score=score, temporal_relation=match.get("temporal_relation", "unknown"),
                source_kind=match.get("source_kind", "unknown"), page_status=match.get("page_status", "unknown")))
            ip_evidence.append(_evidence(post, "rights_review", "文字重合不能证明未经授权，需核对作者归属和许可。", source_url=str(match.get("url", ""))))

        method = str(post.get("creation_method", "")).strip()
        if method and (AUTOMATION.search(method) or HUMAN_METHOD.search(method)):
            automation_checked.add(pid)
        if _automation_declared(method):
            automation_signals.add(pid)
            automation_evidence.append(_evidence(post, "automation_declared", "作者声明涉及自动化创建或发布；声明和实际创作流程尚未核实。", creation_method=method[:160]))
        if complete:
            # This is a topic screening, not an assessment of "entirely centered on".
            topic_checked.add(pid)
            interaction_checked.add(pid)
            text = str(post.get("text", ""))
            if MONETIZATION.search(text) and MONETIZATION_GUIDANCE.search(text):
                topic_signals.add(pid)
                topic_evidence.append(_evidence(post, "monetization_guidance_phrase", "含变现教学或收益提升表述；须通读确认是否完全围绕这一主题。提到收益本身不构成违规。"))
        if "repeated_engagement_phrase" in reasons:
            interaction_signals.add(pid)
            interaction_evidence.append(_evidence(post, "repeated_engagement_phrase", "多条帖子反复请求互动；需看上下文，不直接判断真实互动或原创资格。", source_evidence=reasons["repeated_engagement_phrase"].get("evidence", {})))

    requirements = [
        _item("original_contribution", "原创贡献与实质新视角", "保存释义：认可增加作者视角的解读、分析或背景；照搬、轻微修改、缺乏实质新视角的拼接不被视为该计划下原创。", own,
              original_checked, original_signals, original_evidence, "比例表示外部来源风险线索涉及的记录。查重不能核实作者、新视角或跨语言改写；查无重合也不能证明原创。账号内重复作为参考证据展示，不当作外部搬运。", strength=original_strength),
        _item("automation", "自动化创建或发布", "保存释义：通过自动化方式创建或发布的内容属于不符合奖励条件的情形；保存页面未逐一解释各类 AI 辅助边界。", own,
              automation_checked, automation_signals, automation_evidence, "仅根据明确的创作方式声明提示复核。归档正文通常不能证明创作或发布方式；没有声明时无法判断，AI 辅助不自动等同于自动化生成。", strength="medium"),
        _item("monetization_focus", "完全围绕变现或最大化收益", "保存释义：完全围绕变现教学、变现讨论或最大化收益的内容不符合奖励条件；不扩大为任何提到收益的内容。", own,
              topic_checked, topic_signals, topic_evidence, "这是文字表述的弱线索筛查；未判断整篇是否完全围绕变现，实际适用需要人工通读。单独的收益关键词不会触发风险。", strength="low"),
        _item("intellectual_property", "知识产权与素材使用权", "保存释义：原创内容仍需满足知识产权要求；原创贡献、注明出处与获准使用是不同问题。", own,
              set(), set(), ip_evidence, "没有核验素材作者、许可、权利或授权。文件哈希、引用链接、作者声明和查重结果均不能证明知识产权合规。"),
    ]
    auxiliary = _item("repeated_engagement", "重复互动请求（辅助线索）", None, own,
        interaction_checked, interaction_signals, interaction_evidence, "本工具辅助检查，不是新增的官方原创条款；出现重复请求不等于互动造假，也不构成自动化或原创违规认定。", auxiliary=True)
    result = {"source": dict(SOURCE), "disclaimer": "不符合程度以工具风险线索强度显示；百分比是线索涉及记录占非普通转帖记录的比例，不是官方违规率、抄袭率或评分。未检查、证据不足和未发现线索不能当作符合要求。",
            "denominator_label": "本次非普通转帖记录；包括回复和引用，不能据此认定属于奖励范围。",
            "requirements": [*requirements, auxiliary],
            "account_eligibility": {"status": "unknown", "degree": "证据不足", "reason": "内容报告不能核验会员、认证粉丝、首页时间线曝光、所在地区、账号状态或其他收益资格。", "official_score": None}}
    if isinstance(web_check, dict):
        coverage = web_check.get("coverage", {})
        result["web_evidence_scope"] = {key: coverage.get(key) for key in
            ("mode", "sample_method", "selected_total", "total_eligible", "requested", "selection_search_complete")}
        result["web_evidence_scope"]["projection"] = "not_estimated"
        if coverage.get("mode") == "sample10":
            result["requirements"][0]["interpretation"] += " 联网样本按归档顺序分散抽取，未将样本命中比例推广为全归档违规率。"
        elif coverage.get("mode") == "manual10":
            result["requirements"][0]["interpretation"] += " 联网仅检查在本机归档中手动勾选的 10 条本人主帖正文（包含引用帖，不含回复和普通转帖），未将所选帖子的命中比例推广为全归档违规率；未联网回复仍保留在全部非普通转帖记录的证据不足统计中。"
    return result


def combined_evidence(summary, policy, web_check, posts=None):
    """Combine local and web risk evidence without inventing content judgments."""
    coverage = web_check.get("coverage", {}) if isinstance(web_check, dict) else {}
    known_posts = {str(post.get("id", "")): post for post in posts or []}
    body, snippets, body_signals = set(), set(), set()
    for entry in _web_posts(web_check).values():
        pid = str(entry.get("id", ""))
        post = known_posts.get(pid, {"id": pid})
        for match in entry.get("matches", []):
            score = _match_score(match)
            if score is None or score < 0.5 or _same_post(post, match):
                continue
            if match.get("source_kind") == "page_body" and match.get("page_status") == "fetched":
                body.add(pid)
                if match.get("temporal_relation") != "later":
                    body_signals.add(pid)
            else:
                snippets.add(pid)
    snippets.difference_update(body)  # Count only posts whose overlap has no fetched-body evidence.
    original = next((item for item in policy.get("requirements", []) if item.get("id") == "original_contribution"), {})
    local_review = sum(summary.get("counts", {}).get(status, 0) for status in ("high_risk", "review"))
    requested = coverage.get("requested", 0)
    configured = bool(coverage.get("selection_configured", requested > 0))
    selected = coverage.get("selected_total", coverage.get("total_eligible", 0))
    total = coverage.get("total_eligible", 0)
    unknown = original.get("unknown_count", 0)
    local_only = not configured and not requested
    if local_only:
        selected = 0
        status, title = ("needs_review", "本地分析发现需人工复核的线索") if local_review else ("local_completed", "本地归档检查完成")
    elif not requested:
        status, title = "incomplete", "联网范围已选择，尚未取得公开证据"
    elif body_signals or local_review:
        status, title = "needs_review", "综合证据存在需人工复核的线索"
    elif unknown or not coverage.get("selection_search_complete", coverage.get("search_complete", False)):
        status, title = "incomplete", "综合证据仍有未检查或无法确认的部分"
    else:
        status, title = "no_detected_overlap", "已检查材料未发现明显重合线索"
    mode = coverage.get("mode", "all")
    scope = f"手动勾选的 {selected} 条" if mode == "manual10" else f"分散抽取 {selected} 条" if mode == "sample10" else f"全部 {selected} 条可检索正文"
    if local_only:
        availability = f"共有 {total} 条可联网检索正文，本次未联网。" if total else "当前没有可联网检索的完整正文，本次未联网。"
        conclusion = (f"归档本地分析覆盖 {summary.get('total', 0)} 条记录，其中 {local_review} 条需人工复核。"
                      + availability + f" 原创贡献仍有 {unknown} 条证据不足；归档内相似不能证明抄袭，未发现重复不能证明原创。")
    else:
        conclusion = (f"归档本地分析覆盖 {summary.get('total', 0)} 条记录；联网选择{scope}，已回读成功检索 {coverage.get('searched', 0)} 条。"
                      f"{len(body)} 条取得公开正文重合证据，{len(snippets)} 条仅有摘要重合线索；原创贡献仍有 {unknown} 条证据不足。"
                      "相似不能证明抄袭、作者归属或授权；未发现匹配不能证明原创。")
    if mode in {"sample10", "manual10"} and not local_only:
        conclusion += " 指定帖子结果不能推广为未选帖子或全归档的原创程度。" if mode == "manual10" else " 样本结果不能推广为未选帖子或全归档的原创程度。"
    if coverage.get("execution_unknown_posts", 0):
        conclusion += f" {coverage['execution_unknown_posts']} 条已发出帖子的执行情况未知，不能推断没有消耗查询额度。"
    return {"status": status, "title": title, "conclusion": conclusion,
        "offline_total": summary.get("total", 0), "offline_review_posts": local_review,
        "web_mode": mode, "selected_total": selected, "total_eligible": total,
        "searched_posts": coverage.get("searched", 0), "body_matched_posts": len(body),
        "body_signal_posts": len(body_signals), "snippet_matched_posts": len(snippets),
        "policy_signal_posts": original.get("signal_count", 0), "unknown_own_posts": unknown,
        "unselected_eligible": total if local_only else coverage.get("unselected_eligible", max(total - selected, 0)),
        "remaining": 0 if local_only else coverage.get("remaining", selected),
        "sample_method": "not_selected" if local_only else coverage.get("sample_method", "all_eligible"),
        "selection_configured": configured,
        "selection_complete": coverage.get("selection_complete", False),
        "all_eligible_requested": coverage.get("all_eligible_requested", False),
        "execution_unknown_posts": coverage.get("execution_unknown_posts", 0),
        "unknown_execution_chars": coverage.get("unknown_execution_chars", 0)}
