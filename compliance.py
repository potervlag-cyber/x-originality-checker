"""Evidence-weighted content compliance, with unknown evidence kept explicit.

Weights are tool choices. Scores come only from validated content reviews, never
post counts, duplicate rates, search overlap scores or material completeness.
"""
from __future__ import annotations

from collections import Counter
import math

CRITERIA = (
    ("original_contribution", "原创贡献与实质新视角"),
    ("automation", "自动化创建或发布"),
    ("monetization_focus", "是否完全围绕变现"),
    ("intellectual_property", "知识产权"),
)
CRITERION_IDS = tuple(identifier for identifier, _ in CRITERIA)
DEFAULT_WEIGHTS = dict(zip(CRITERION_IDS, (50, 20, 10, 20)))
UNVERIFIABLE = {"automation", "intellectual_property"}


def validate_weights(value):
    if not isinstance(value, dict) or set(value) != set(CRITERION_IDS):
        raise ValueError("权重必须包含四项固定原创要求，且不能添加其他项。")
    if any(isinstance(weight, bool) or not isinstance(weight, (int, float))
           or not math.isfinite(weight) or not 0 <= weight <= 100 for weight in value.values()):
        raise ValueError("每项权重应为 0–100 的有限数字。")
    if value["original_contribution"] <= 0:
        raise ValueError("原创贡献权重必须大于 0，不能只用主题评估生成综合符合率。")
    if not math.isclose(sum(value.values()), 100, rel_tol=0, abs_tol=1e-8):
        raise ValueError("四项权重合计必须为 100。")
    return {identifier: value[identifier] for identifier in CRITERION_IDS}


def unknown_content_review(status="not_configured"):
    return {"schema_version": 1, "status": status, "model": "", "issues": [], "criteria": [
        {"id": identifier, "score": None, "verdict": "unknown",
         "rationale": "未取得可验证的内容评估。", "post_excerpt": "", "source_url": "", "source_excerpt": ""}
        for identifier in CRITERION_IDS]}


def _score(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100 else None


def assess_compliance(posts, web_check=None, *, selected_ids=None, weights=None):
    weights = validate_weights(DEFAULT_WEIGHTS if weights is None else weights)
    posts = list(posts or [])
    lookup = {str(post.get("id", "")): post for post in posts}
    if selected_ids is None:
        scope = [post for post in posts if post.get("type") != "repost"]
        mode, ids = "local_non_repost", []
    else:
        ids = list(selected_ids)
        if len(set(ids)) != len(ids) or any(pid not in lookup for pid in ids):
            raise ValueError("符合率范围必须是当前归档中的不同帖子编号。")
        scope = [lookup[pid] for pid in ids]
        mode = "selected10"
    entries = {str(post.get("id", "")): post for post in (web_check or {}).get("posts", [])}
    denominator = len(scope)
    rows, model_names = [], set()
    covered_weight = supported_weight = 0.0
    status_counts = Counter()
    for post in scope:
        review = entries.get(str(post.get("id", "")), {}).get("content_review")
        status_counts[review.get("status", "failed") if isinstance(review, dict) else ("not_returned" if ids else "not_requested")] += 1
        if isinstance(review, dict) and review.get("model"):
            model_names.add(review["model"])
    for identifier, title in CRITERIA:
        scores, evidence, unknown_reasons = [], [], Counter()
        evidence_total = 0
        for post in scope:
            pid = str(post.get("id", ""))
            review = entries.get(pid, {}).get("content_review")
            criterion = next((item for item in review.get("criteria", []) if isinstance(item, dict) and item.get("id") == identifier), None) if isinstance(review, dict) else None
            score = _score(criterion.get("score")) if criterion and review.get("status") == "completed" and identifier not in UNVERIFIABLE else None
            if score is not None:
                scores.append(score)
            else:
                reason = "创作过程/权利授权证据不足" if identifier in UNVERIFIABLE else (
                    "模型未配置" if isinstance(review, dict) and review.get("status") == "not_configured" else
                    "内容评估失败" if isinstance(review, dict) and review.get("status") == "failed" else
                    "本条要求证据不足" if isinstance(review, dict) else "未取得内容评估")
                unknown_reasons[reason] += 1
            if criterion:
                evidence_total += 1
                if len(evidence) < 4:
                    evidence.append({"post_id": pid, "score": score, "verdict": criterion.get("verdict", "unknown"),
                        "rationale": criterion.get("rationale", ""), "post_excerpt": criterion.get("post_excerpt", ""),
                        "source_url": criterion.get("source_url", ""), "source_excerpt": criterion.get("source_excerpt", ""),
                        "review_status": review.get("status"), "model": review.get("model", "")})
        coverage = len(scores) / denominator if denominator else 0
        average = sum(scores) / len(scores) if scores else None
        covered_weight += weights[identifier] * coverage
        if average is not None:
            supported_weight += weights[identifier] * coverage * average / 100
        rows.append({"id": identifier, "title": title, "weight": weights[identifier],
            "score": round(average, 2) if average is not None else None,
            "evaluated_count": len(scores), "unknown_count": denominator - len(scores),
            "denominator": denominator, "coverage_percent": round(coverage * 100, 2),
            "evidence": evidence, "evidence_total": evidence_total, "unknown_reasons": dict(unknown_reasons),
            "strength": "内容模型参考判断" if scores else "证据不足"})
    original_known = rows[0]["evaluated_count"] > 0
    score = supported_weight / covered_weight * 100 if covered_weight > 0 and original_known else None
    unknown = max(0, 100 - covered_weight)
    return {"score": round(score, 2) if score is not None else None,
        "label": "已评估部分加权符合率", "coverage_percent": round(covered_weight, 2),
        "supported_percent": round(supported_weight, 2), "unknown_weight": round(unknown, 2),
        "evidence_range": {"low": round(supported_weight, 2), "high": round(min(100, supported_weight + unknown), 2)},
        "weights": weights, "weights_basis": "工具自定权重，可调整；不是 X 官方权重。",
        "evidence_scope": {"mode": mode, "scope_count": denominator, "selected_ids": ids,
            "projection": "not_estimated", "label": "仅本次选择的 10 条主帖" if ids else "全部非普通转帖归档记录；尚无实质内容评估"},
        "criteria": rows, "models": sorted(model_names), "review_statuses": dict(status_counts),
        "status": "insufficient" if score is None else "partially_assessed" if unknown else "assessed",
        "strength": "证据不足" if score is None else "已评估内容的模型参考判断，仍需复核",
        "official_score": None,
        "explanation": "符合率仅汇总有依据的内容评估，未知项不加分或扣分；原创贡献没有内容判断时不显示符合率。证据范围来自未知项取最低/最高分的上下限，不是置信区间或通过概率。数量、重复率、媒体缺失和弱查重线索均不直接计分。所选 10 条不外推为全归档或全网。"}
