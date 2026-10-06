import unittest

from engine import analyze


ORIGINAL = "我在同一台设备上连续测试了三种发布方式，记录每次加载延迟和失败原因。结果显示完整来源说明减少了后续核实时间，以下是测量过程与具体结论。"
SOURCE = "供应链系统应当协调需求预测与库存策略，在波动较大的情况下采用滚动计划，并根据实际订单调整生产和运输安排，从而减少不必要的积压和缺货。"


def project(posts, **scope):
    return {"account": "@demo", "posts": posts, "scope": scope}


class EngineTests(unittest.TestCase):
    def test_exact_duplicate_cannot_be_recommended(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL}, {"id": "b", "text": ORIGINAL + " https://example.test/a"}]))
        self.assertEqual({"high_risk": 2, "review": 0, "insufficient": 0, "low_signal": 0}, result["summary"]["counts"])
        self.assertEqual([], result["candidates"])
        self.assertTrue(all(any(r["code"] == "internal_exact" for r in p["reasons"]) for p in result["posts"]))

    def test_near_rewrite_review_but_short_greetings_not_flagged(self):
        changed = ORIGINAL.replace("连续测试", "反复测试").replace("加载延迟", "响应延迟")
        result = analyze(project([{"id": "a", "text": ORIGINAL}, {"id": "b", "text": changed}, {"id": "c", "text": "早上好"}, {"id": "d", "text": "早上好"}]))
        self.assertEqual("review", result["posts"][0]["status"])
        self.assertEqual("review", result["posts"][1]["status"])
        self.assertEqual("low_signal", result["posts"][2]["status"])
        self.assertFalse(result["posts"][2]["candidate_eligible"])

    def test_source_copy_risk_does_not_apply_blindly_to_commentary_or_self(self):
        contribution = "我另外收集了三个月的订单和运输数据，比较两种策略的缺货次数并分析季节波动带来的局限，这些结果已写入正文。"
        records = [
            {"id": "copy", "text": SOURCE, "sources": [{"text": SOURCE, "owner": "other"}]},
            {"id": "commentary", "text": ORIGINAL, "sources": [{"text": SOURCE, "owner": "other"}], "contribution": contribution, "type": "quote"},
            {"id": "self", "text": SOURCE, "sources": [{"text": SOURCE, "owner": "self"}]},
        ]
        # Test independently so account-internal duplication does not hide the source logic.
        statuses = {r["id"]: analyze(project([r]))["posts"][0]["status"] for r in records}
        self.assertEqual("high_risk", statuses["copy"])
        self.assertEqual("low_signal", statuses["commentary"])
        self.assertEqual("review", statuses["self"])

    def test_overlap_with_contribution_requires_review(self):
        result = analyze(project([{"id": "a", "text": SOURCE, "sources": [{"text": SOURCE, "owner": "third_party"}],
                                   "contribution": "我进行了独立测量并给出两种方法的误差和使用条件，同时补充失败案例，说明不同规模下应该如何调整策略。"}]))
        self.assertEqual("review", result["posts"][0]["status"])
        self.assertTrue(any(r["code"] == "source_overlap_with_contribution" for r in result["posts"][0]["reasons"]))

    def test_links_are_unchecked_and_media_names_never_passed(self):
        result = analyze(project([{"id": "a", "url": "https://x.com/demo/status/1", "text": ORIGINAL,
                                   "sources": [{"url": "https://source.test"}], "media": [{"name": "one.png"}]}]))
        self.assertEqual("insufficient", result["posts"][0]["status"])
        self.assertEqual(0, result["coverage"]["links_visited"])
        self.assertEqual(0, result["coverage"]["source_text_comparisons"])
        self.assertEqual(1, result["coverage"]["source_links_unchecked"])
        self.assertEqual(0, result["coverage"]["hashed_media"])

    def test_media_hash_repeat_is_review_not_ownership_claim(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL, "media": [{"name": "a.jpg", "hash": "f" * 64}]},
                                   {"id": "b", "text": SOURCE, "media": [{"name": "b.jpg", "hash": "f" * 64}]}]))
        self.assertEqual(2, result["coverage"]["hashed_media"])
        self.assertEqual(2, result["summary"]["counts"]["review"])
        self.assertTrue(all(any(r["code"] == "media_same_hash" for r in p["reasons"]) for p in result["posts"]))

    def test_missing_thread_detected(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL, "thread_id": "t", "thread_order": 1, "thread_total": 3},
                                   {"id": "b", "text": SOURCE, "thread_id": "t", "thread_order": 3, "thread_total": 3}]))
        self.assertEqual(2, result["summary"]["counts"]["insufficient"])
        self.assertTrue(all(any(r["code"] == "thread_incomplete" for r in p["reasons"]) for p in result["posts"]))

    def test_scope_inclusive_in_declared_timezone_and_unknown_excluded(self):
        result = analyze(project([
            {"id": "on_start", "text": ORIGINAL, "created_at": "2026-07-08T16:00:00Z"},
            {"id": "on_end", "text": SOURCE, "created_at": "2026-10-06T15:59:59Z"},
            {"id": "after", "text": "稍晚的一篇", "created_at": "2026-10-06T16:00:00Z"},
            {"id": "unknown", "text": "时间未知", "created_at": "未知"}],
            start="2026-07-09", end="2026-10-06", timezone="Asia/Shanghai", total_expected=10))
        self.assertEqual(2, result["summary"]["total"])
        self.assertEqual(["after"], result["coverage"]["outside_scope"])
        self.assertEqual(["unknown"], result["coverage"]["invalid_date"])
        self.assertEqual("2026-07-09", result["coverage"]["actual_start"])
        self.assertIsNone(result["coverage"]["coverage_percent"])
        self.assertEqual(["on_start", "on_end"], [p["id"] for p in result["filtered_project"]["posts"]])

    def test_bad_date_range_and_timezone_not_silently_used(self):
        for scope in ({"start": "tomorrow"}, {"start": "2026-10-06", "end": "2026-01-01"}, {"timezone": "UTC+30"}):
            with self.assertRaises(ValueError):
                analyze(project([], **scope))

    def test_unknown_total_no_precise_coverage_and_false_completeness_preserved(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL}], complete=False))
        self.assertIsNone(result["coverage"]["coverage_percent"])
        self.assertFalse(result["coverage"]["scope"]["complete"])
        self.assertTrue(result["coverage"]["notes"])

    def test_candidates_max_ten_and_no_reply_repost_or_missing_text(self):
        records = [{"id": str(i), "text": "我调查的独立项目" + str(i) + "。" + chr(0x4e00 + i) * 70} for i in range(13)]
        records += [{"id": "reply", "text": ORIGINAL, "type": "reply"}, {"id": "repost", "text": SOURCE, "type": "repost"}, {"id": "no-text", "url": "https://x.com/status/1"}]
        result = analyze(project(records))
        self.assertEqual(10, len(result["candidates"]))
        self.assertEqual(10, len({p["id"] for p in result["candidates"]}))
        self.assertFalse({"reply", "repost", "no-text"} & {p["id"] for p in result["candidates"]})
        self.assertEqual(result["summary"]["total"], sum(result["summary"]["counts"].values()))

    def test_engagement_only_repeated_explicit_phrase(self):
        one = analyze(project([{"id": "a", "text": ORIGINAL + "关注并转发"}]))
        self.assertFalse(any(r["code"] == "repeated_engagement_phrase" for r in one["findings"]))
        many = analyze(project([{"id": str(i), "text": chr(0x4e00 + i) * 50 + "关注并转发"} for i in range(3)]))
        self.assertEqual(3, sum(r["code"] == "repeated_engagement_phrase" for r in many["findings"]))

    def test_ai_label_never_direct_violation(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL, "creation_method": "自动生成或自动发布"}]))
        self.assertEqual("low_signal", result["posts"][0]["status"])
        self.assertTrue(result["posts"][0]["candidate_eligible"])

    def test_explicit_unknown_completeness_blocks_recommendation(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL, "text_complete": "不确定"}]))
        self.assertEqual("insufficient", result["posts"][0]["status"])
        self.assertEqual([], result["candidates"])

    def test_complete_thread_recommends_only_earliest_entry(self):
        result = analyze(project([{"id": "later", "text": ORIGINAL, "thread_id": "t", "thread_order": 2, "thread_total": 2, "thread_complete": True},
                                   {"id": "entry", "text": SOURCE, "thread_id": "t", "thread_order": 1, "thread_total": 2, "thread_complete": True}]))
        self.assertEqual(["entry"], [p["id"] for p in result["candidates"]])
        self.assertEqual(2, result["summary"]["counts"]["low_signal"])

    def test_complete_claim_cannot_override_declared_missing_thread_parts(self):
        result = analyze(project([{"id": "entry", "text": ORIGINAL, "thread_id": "t", "thread_order": 1, "thread_total": 3, "thread_complete": True}]))
        self.assertEqual("insufficient", result["posts"][0]["status"])
        self.assertEqual([], result["candidates"])

    def test_no_in_scope_posts_is_not_clean_conclusion(self):
        result = analyze(project([{"id": "a", "text": ORIGINAL, "created_at": "未知"}], start="2026-07-09", end="2026-10-06"))
        self.assertEqual(0, result["summary"]["total"])
        self.assertIn("没有可评估", result["summary"]["account_conclusion"])
        self.assertNotIn("未发现", result["summary"]["account_conclusion"])


if __name__ == "__main__":
    unittest.main()
