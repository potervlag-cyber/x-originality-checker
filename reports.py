"""Standalone report rendering shared by desktop and browser runtimes."""
import html
import json
from datetime import datetime, timezone
from engine import VERSION


def markdown_report(project: dict, result: dict) -> str:
    def line(value):
        return str(value or "").replace("\r", " ").replace("\n", " ")

    summary = result.get("summary", {})
    coverage = result.get("coverage", {})
    lines = [
        "# X 原创风险评估报告",
        "",
        f"- 工具版本：{VERSION}",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）",
        f"- 账号：{line(project.get('account')) or '未填写'}",
        "- 评估方式：本机规则与已提供材料对比，未调用 X 官方审核接口或全网查重。",
        "- 结论边界：风险等级不是官方原创认定、申请通过概率或版权结论。",
        "",
        "## 覆盖范围",
        "",
        "```json",
        json.dumps(coverage, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 检查概览",
        "",
        f"实际评估帖子：{summary.get('total', 0)}",
        "",
    ]
    labels = {"high_risk": "高风险", "review": "需复核", "insufficient": "材料不足", "low_signal": "未发现明显文本风险"}
    for status, label in labels.items():
        lines.append(f"- {label}：{summary.get('counts', {}).get(status, 0)}")
    lines.extend(["", "## 逐帖证据", ""])
    for post in result.get("posts", []):
        lines.extend([
            f"### {line(post.get('id'))} · {line(post.get('status_label'))}",
            "",
            f"链接：{line(post.get('url')) or '未提供'}",
            "",
            f"时间：{line(post.get('created_at')) or '未提供'}；类型：{line(post.get('type'))}",
            "",
            "原文：",
            "",
        ])
        # Quote imported text so headings or embedded HTML cannot become report structure.
        lines.extend("> " + text_line for text_line in str(post.get("text", "")).replace("\r", "").split("\n"))
        lines.append("")
        for reason in post.get("reasons", []):
            lines.append(f"- {line(reason.get('message'))}")
            if reason.get("evidence"):
                lines.append(f"  - 对比证据：{line(reason.get('evidence'))}")
        lines.extend(["", f"候选说明：{line(post.get('candidate_reason')) or '未推荐'}", ""])
    lines.extend(["## 申请候选", "", "候选顺序为本工具的材料排序，不是官方评分。未找到足够候选时不凑满 10 篇。", ""])
    selected = set(project.get("selected_candidates", []))
    for candidate in result.get("candidates", []):
        mark = "（已选）" if candidate.get("id") in selected else ""
        lines.append(f"- {line(candidate.get('rank'))}. {line(candidate.get('id'))} {line(candidate.get('url'))} {mark}")
    if selected:
        lines.extend(["", "用户已选编号：" + "、".join(line(item) for item in project.get("selected_candidates", []))])
    lines.extend(["", "## 局限与待确认", ""])
    lines.extend("- " + line(value) for value in result.get("limitations", []))
    lines.extend([
        "",
        "公开规则来源：",
        "",
        "- https://help.x.com/en/using-x/original-content-rewards",
        "- https://help.x.com/en/rules-and-policies/authenticity",
        "- https://help.x.com/en/rules-and-policies/copyright-policy",
        "",
    ])
    return "\n".join(lines)


def html_report(project: dict, result: dict) -> str:
    escape = lambda value: html.escape(str(value or ""))
    summary = result.get("summary", {})
    coverage = result.get("coverage", {})
    cards = []
    for post in result.get("posts", []):
        reasons = "".join(
            "<li>" + escape(reason.get("message")) +
            ("<pre>" + escape(reason.get("evidence")) + "</pre>" if reason.get("evidence") else "") + "</li>"
            for reason in post.get("reasons", [])
        )
        cards.append(f"<section><h2>{escape(post.get('id'))} · {escape(post.get('status_label'))}</h2>"
                     f"<p>{escape(post.get('url'))}</p><p>{escape(post.get('created_at'))} · {escape(post.get('type'))}</p>"
                     f"<pre>{escape(post.get('text'))}</pre><ul>{reasons}</ul>"
                     f"<p class='muted'>候选说明：{escape(post.get('candidate_reason'))}</p></section>")
    candidates = "".join(f"<li>{escape(p.get('id'))} · {escape(p.get('url'))}</li>" for p in result.get("candidates", []))
    limitations = "".join("<li>" + escape(item) + "</li>" for item in result.get("limitations", []))
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>X 原创风险评估报告</title>
<style>body{{max-width:960px;margin:40px auto;padding:0 24px;font:15px/1.8 'Microsoft YaHei',sans-serif;color:#203544;background:#f5f8fa}}h1{{font-size:28px}}h2{{font-size:19px}}section{{background:white;padding:22px;margin:18px 0;border:1px solid #dbe5ec;border-radius:12px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;background:#f3f6f8;padding:14px}}.muted{{color:#536b7b}}@media print{{body{{margin:0;background:white}}section{{break-inside:avoid}}}}</style>
<h1>X 原创风险评估报告</h1><p>账号：{escape(project.get('account')) or '未填写'} · 版本 {VERSION}</p>
<p class="muted">本机材料风险评估，未调用官方审核或全网查重。结果不是官方原创认定、申请通过概率或版权结论。</p>
<section><h2>覆盖范围</h2><pre>{escape(json.dumps(coverage, ensure_ascii=False, indent=2))}</pre>
<h2>检查概览</h2><p>评估 {escape(summary.get('total', 0))} 篇</p><pre>{escape(json.dumps(summary.get('counts', {}), ensure_ascii=False, indent=2))}</pre></section>
{''.join(cards)}<section><h2>申请候选</h2><p>材料排序，不是官方评分；不足 10 篇时不凑数。</p><ol>{candidates}</ol>
<p>用户已选：{escape('、'.join(project.get('selected_candidates', []))) or '无'}</p></section>
<section><h2>局限与待确认</h2><ul>{limitations}</ul></section></html>"""
