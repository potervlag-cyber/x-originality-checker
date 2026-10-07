import unittest

from engine import analyze
from importers import normalize_project
from policy_checks import assess_policy, combined_evidence


BODY = "我在同一台设备上测试三种资料整理方法，记录每次出现的失败、修正过程和结果，并且把适用条件与不能确认的结论分别写清楚。"


def normalized(records):
    return normalize_project({"posts": records})["posts"]


def row(result, identifier):
    return next(p for p in result["requirements"] if p["id"] == identifier)


class PolicyChecksTests(unittest.TestCase):
    def test_saved_reference_is_not_reported_live_or_complete(self):
        result = assess_policy([])
        self.assertEqual("2026-10-06", result["source"]["reference_date"])
        self.assertEqual("unavailable", result["source"]["live_verification"])
        self.assertEqual("2026-10-07", result["source"]["live_attempt_date"])
        self.assertIn("不是官方完整清单", result["source"]["note"])
        self.assertEqual("unknown", result["account_eligibility"]["status"])
        for item in result["requirements"]:
            self.assertEqual("无法判断", item["degree"])
            self.assertIsNone(item["signal_percent"])
            self.assertIsNone(item["official_score"])

    def test_no_external_evidence_keeps_originality_and_automation_unknown(self):
        posts = normalized([{"id": "1", "text": BODY}])
        result = assess_policy(posts, analyze({"posts": posts}))
        for identifier in ("original_contribution", "automation", "intellectual_property"):
            item = row(result, identifier)
            self.assertEqual("unknown", item["status"])
            self.assertEqual(1, item["unknown_count"])
            self.assertIsNone(item["signal_percent"])

    def test_self_repetition_is_reference_not_external_copying_signal(self):
        posts = normalized([{"id": "1", "text": BODY}, {"id": "2", "text": BODY}])
        item = row(assess_policy(posts, analyze({"posts": posts})), "original_contribution")
        self.assertEqual("unknown", item["status"])
        self.assertEqual(0, item["signal_count"])
        self.assertIsNone(item["signal_percent"])
        self.assertEqual(2, item["evidence_total"])
        self.assertTrue(all(e["code"] == "internal_exact" for e in item["evidence"]))

    def test_supplied_other_copy_is_review_signal_and_not_official_score(self):
        posts = normalized([{"id": "copy", "text": BODY, "sources": [{"text": BODY, "owner": "other"}]}])
        result = assess_policy(posts, analyze({"posts": posts}))
        item = row(result, "original_contribution")
        self.assertEqual("signals_found", item["status"])
        self.assertEqual("中（需复核）", item["degree"])
        self.assertEqual(100.0, item["signal_percent"])
        self.assertEqual(0, item["unknown_count"])
        self.assertIsNone(item["official_noncompliance_percent"])
        self.assertEqual("unknown", row(result, "intellectual_property")["status"])

    def test_reposts_are_not_counted_as_violations_or_own_denominator(self):
        posts = normalized([{"id": "1", "text": BODY}, {"id": "2", "text": "RT @someone: " + BODY, "type": "repost"}])
        result = assess_policy(posts, analyze({"posts": posts}))
        self.assertTrue(all(p["denominator"] == 1 for p in result["requirements"]))
        self.assertEqual(0, row(result, "original_contribution")["signal_count"])

    def test_only_explicit_creation_method_can_raise_automation_signal(self):
        posts = normalized([
            {"id": "1", "text": BODY + "我讨论了自动生成的文章。"},
            {"id": "2", "text": BODY, "creation_method": "工具辅助"},
            {"id": "3", "text": BODY, "creation_method": "AI生成，人工点击发布"},
            {"id": "4", "text": BODY, "creation_method": "自动发布"},
        ])
        item = row(assess_policy(posts), "automation")
        self.assertEqual(2, item["signal_count"])
        self.assertEqual(2, item["assessed_count"])
        self.assertEqual(2, item["unknown_count"])
        self.assertEqual(50.0, item["signal_percent"])
        self.assertEqual({"3", "4"}, {e["post_id"] for e in item["evidence"]})

    def test_negated_automation_is_not_positive_but_actual_other_action_is(self):
        posts = normalized([
            {"id": "1", "text": BODY, "creation_method": "本人创作，并非AI生成；未使用自动发布"},
            {"id": "2", "text": BODY, "creation_method": "不是自动生成，但是自动发布"},
        ])
        item = row(assess_policy(posts), "automation")
        self.assertEqual(1, item["signal_count"])
        self.assertEqual("2", item["evidence"][0]["post_id"])

    def test_revenue_mention_alone_does_not_raise_monetization_signal(self):
        posts = normalized([
            {"id": "1", "text": "这次设备测量能减少能源消耗，也帮助理解投入成本和长期收益。这里记录具体方法、测量误差与适用条件。"},
            {"id": "2", "text": "如何提高X创作者收益：这篇介绍变现教程与收益技巧，整理公开的规则，并讨论需要另外核对的风险。"},
        ])
        item = row(assess_policy(posts), "monetization_focus")
        self.assertEqual(1, item["signal_count"])
        self.assertEqual("低（仅弱线索）", item["degree"])
        self.assertEqual("2", item["evidence"][0]["post_id"])
        self.assertIn("须通读", item["evidence"][0]["detail"])

    def test_short_or_incomplete_bodies_are_unknown_not_zero_percent(self):
        posts = normalized([
            {"id": "1", "text": "如何变现？"},
            {"id": "2", "text": BODY, "text_complete": False},
        ])
        item = row(assess_policy(posts), "monetization_focus")
        self.assertEqual("unknown", item["status"])
        self.assertIsNone(item["signal_percent"])
        self.assertEqual(2, item["unknown_count"])

    def test_unknown_web_author_match_is_signal_not_verified_copying(self):
        posts = normalized([{"id": "1", "text": BODY}])
        web = {"posts": [{"id": "1", "status": "matched", "sources_checked": 1, "matches": [
            {"url": "https://example.test/story", "score": 0.92, "temporal_relation": "unknown", "source_kind": "page_body", "page_status": "fetched"}
        ]}]}
        result = assess_policy(posts, web_check=web)
        item = row(result, "original_contribution")
        self.assertEqual("signals_found", item["status"])
        self.assertEqual("中（需复核）", item["degree"])
        self.assertIn("不能直接认定抄袭", item["evidence"][0]["detail"])
        self.assertIsNone(row(result, "intellectual_property")["signal_percent"])

    def test_same_x_post_later_source_and_invalid_scores_are_excluded(self):
        posts = normalized([{"id": "123456789", "text": BODY}])
        matches = [
            {"url": "https://x.com/someone/status/123456789", "score": 1},
            {"url": "https://twitter.com/other/status/123456789/photo/1", "score": 1},
            {"url": "https://example.test/later", "score": 0.98, "temporal_relation": "later"},
            {"url": "https://example.test/invalid", "score": 98},
            {"url": "https://example.test/boolean", "score": True},
        ]
        web = {"posts": [{"id": "123456789", "status": "matched", "sources_checked": 1, "matches": matches}]}
        item = row(assess_policy(posts, web_check=web), "original_contribution")
        self.assertEqual(0, item["signal_count"])
        self.assertEqual([], item["evidence"])

    def test_snippet_match_reports_source_and_unknown_coverage(self):
        posts = normalized([{"id": "1", "text": BODY}])
        web = {"posts": [{"id": "1", "status": "partial", "sources_checked": 0, "matches": [
            {"url": "https://example.test/story", "score": 0.9, "source_kind": "search_snippet", "page_status": "source_timeout"}
        ]}]}
        item = row(assess_policy(posts, web_check=web), "original_contribution")
        self.assertEqual("signals_found", item["status"])
        self.assertEqual(1, item["unknown_count"])
        self.assertEqual("低（仅弱线索）", item["degree"])
        self.assertIn("仅搜索摘要", item["evidence"][0]["detail"])

    def test_media_rights_and_originality_remain_unchecked(self):
        posts = normalized([{"id": "1", "text": BODY, "media": [{"name": "photo.png", "hash": "a" * 64}],
                             "sources": [{"text": BODY, "owner": "self"}]}])
        result = assess_policy(posts, analyze({"posts": posts}))
        self.assertEqual(1, row(result, "original_contribution")["unknown_count"])
        self.assertEqual("unknown", row(result, "intellectual_property")["status"])

    def test_long_post_partial_web_text_keeps_unchecked_originality(self):
        posts = normalized([{"id": "long", "text": "内容案例" * 1600}])
        for counts in (
            {"original_chars": 6400, "checked_chars": 5000, "text_truncated": True},
            {"original_chars": 6400, "checked_chars": 5000, "text_truncated": False},
            {"text_truncated": True},
        ):
            with self.subTest(counts=counts):
                web = {"posts": [{"id": "long", "status": "no_match", "sources_checked": 1,
                                  "matches": [], **counts}]}
                item = row(assess_policy(posts, web_check=web), "original_contribution")
                self.assertEqual("unknown", item["status"])
                self.assertEqual(0, item["assessed_count"])
                self.assertEqual(1, item["unknown_count"])
                self.assertIsNone(item["signal_percent"])

    def test_complete_web_character_counts_keep_assessed_coverage(self):
        posts = normalized([{"id": "1", "text": BODY}])
        web = {"posts": [{"id": "1", "status": "no_match", "sources_checked": 1, "matches": [],
                          "original_chars": len(BODY), "checked_chars": len(BODY), "text_truncated": False}]}
        item = row(assess_policy(posts, web_check=web), "original_contribution")
        self.assertEqual("no_detected_signal", item["status"])
        self.assertEqual(1, item["assessed_count"])
        self.assertEqual(0, item["unknown_count"])

    def test_auxiliary_requests_do_not_become_an_official_originality_clause(self):
        posts = normalized([{"id": str(i), "text": BODY + "请大家关注并转发。"} for i in range(3)])
        result = assess_policy(posts, analyze({"posts": posts}))
        item = row(result, "repeated_engagement")
        self.assertEqual("auxiliary", item["scope"])
        self.assertIsNone(item["official_requirement"])
        self.assertEqual(3, item["signal_count"])
        self.assertEqual(1, sum(r["id"] == "repeated_engagement" for r in result["requirements"]))
        self.assertNotIn("auxiliary_checks", result)
        self.assertIn("非普通转帖", result["denominator_label"])

    def test_signal_counts_and_evidence_are_bounded_independently(self):
        posts = normalized([{"id": str(i), "text": BODY, "creation_method": "自动发布"} for i in range(12)])
        item = row(assess_policy(posts), "automation")
        self.assertEqual(12, item["signal_count"])
        self.assertEqual(12, item["evidence_total"])
        self.assertEqual(4, len(item["evidence"]))
        self.assertEqual({"0", "1", "2", "3"}, {e["post_id"] for e in item["evidence"]})

    def test_large_internal_group_keeps_exact_total_and_only_sample_evidence(self):
        posts = normalized([{"id": str(i), "text": BODY} for i in range(12)])
        assessed = [{**post, "reasons": [{"code": "internal_exact"}]} for post in posts]
        item = row(assess_policy(posts, assessed), "original_contribution")
        self.assertEqual(12, item["evidence_total"])
        self.assertEqual(4, len(item["evidence"]))
        self.assertEqual(0, item["signal_count"])
        self.assertEqual(12, item["unknown_count"])
        self.assertIsNone(item["signal_percent"])

    def test_manual_ten_evidence_does_not_change_population_denominator_or_invent_probability(self):
        posts = normalized([{"id": str(index), "text": BODY + str(index)} for index in range(20)])
        web = {"coverage": {"mode": "manual10", "selected_total": 10, "total_eligible": 20,
                "requested": 1, "searched": 1, "remaining": 9, "unselected_eligible": 10,
                "sample_method": "manual_archive_selection", "selection_search_complete": False},
               "posts": [{"id": "0", "status": "matched", "sources_checked": 1,
                "matches": [{"url": "https://example.test/source", "score": 0.93,
                             "source_kind": "page_body", "page_status": "fetched", "temporal_relation": "unknown"}]}]}
        policy = assess_policy(posts, web_check=web)
        original = row(policy, "original_contribution")
        self.assertEqual((1, 20, 5.0), (original["signal_count"], original["denominator"], original["signal_percent"]))
        self.assertEqual("not_estimated", policy["web_evidence_scope"]["projection"])
        self.assertIn("手动勾选的 10 条", original["interpretation"])
        self.assertIn("不含回复和普通转帖", original["interpretation"])
        self.assertIn("未联网回复", original["interpretation"])
        combined = combined_evidence({"total": 20, "counts": {}}, policy, web, posts)
        self.assertEqual("needs_review", combined["status"])
        self.assertEqual((1, 0, 19), (combined["body_matched_posts"], combined["snippet_matched_posts"], combined["unknown_own_posts"]))
        self.assertIsNone(combined["official_probability"])
        self.assertFalse(combined["probability_recalculated"])
        self.assertIn("手动勾选的 10 条", combined["conclusion"])
        self.assertNotIn("分散抽取", combined["conclusion"])

    def test_snippets_later_sources_and_same_post_never_become_strong_copying_conclusion(self):
        posts = normalized([{"id": "123456789", "text": BODY}])
        for match in (
            {"url": "https://example.test/source", "source_kind": "search_snippet", "page_status": "source_http_failed"},
            {"url": "https://example.test/later", "source_kind": "page_body", "page_status": "fetched", "temporal_relation": "later"},
            {"url": "https://x.com/another/status/123456789", "source_kind": "page_body", "page_status": "fetched"},
        ):
            with self.subTest(match=match):
                web = {"coverage": {"requested": 1, "selected_total": 1, "total_eligible": 1, "searched": 1, "selection_search_complete": False},
                       "posts": [{"id": "123456789", "status": "partial", "sources_checked": 0, "matches": [{"score": 0.96, **match}]}]}
                combined = combined_evidence({"total": 1, "counts": {}}, assess_policy(posts, web_check=web), web, posts)
                self.assertEqual(0, combined["body_signal_posts"])
                self.assertEqual("incomplete", combined["status"])

    def test_post_with_body_and_snippet_is_not_counted_as_snippet_only(self):
        posts = normalized([{"id": "1", "text": BODY}, {"id": "2", "text": BODY + "第二条"}])
        snippet = {"url": "https://example.test/snippet", "score": 0.94, "source_kind": "search_snippet", "page_status": "source_http_failed"}
        body = {"url": "https://example.test/body", "score": 0.94, "source_kind": "page_body", "page_status": "fetched"}
        web = {"coverage": {"requested": 2, "selected_total": 2, "total_eligible": 2, "searched": 2},
               "posts": [{"id": "1", "status": "partial", "sources_checked": 1, "matches": [body, snippet]},
                         {"id": "2", "status": "partial", "sources_checked": 0, "matches": [snippet]}]}
        combined = combined_evidence({"total": 2, "counts": {}}, assess_policy(posts, web_check=web), web, posts)
        self.assertEqual(1, combined["body_matched_posts"])
        self.assertEqual(1, combined["snippet_matched_posts"])
        self.assertIn("1 条仅有摘要", combined["conclusion"])

    def test_local_only_conclusion_leads_with_local_findings_and_does_not_select_web_pool(self):
        posts = normalized([{"id": "1", "text": BODY}])
        web = {"coverage": {"requested": 0, "selected_total": 1, "total_eligible": 1,
                "remaining": 1, "selection_configured": False}, "posts": []}
        policy = assess_policy(posts)
        for counts, status in (({"high_risk": 1}, "needs_review"), ({}, "local_completed")):
            with self.subTest(counts=counts):
                combined = combined_evidence({"total": 1, "counts": counts}, policy, web, posts)
                self.assertEqual(status, combined["status"])
                self.assertIn("本地", combined["title"])
                self.assertIn("本次未联网", combined["conclusion"])
                self.assertIn("共有 1 条可联网检索", combined["conclusion"])
                self.assertNotIn("联网选择", combined["conclusion"])
                self.assertEqual((0, 0, 1), (combined["selected_total"], combined["remaining"], combined["unselected_eligible"]))
                self.assertFalse(combined["selection_configured"])
                self.assertEqual("not_selected", combined["sample_method"])
        web["coverage"]["total_eligible"] = 0
        combined = combined_evidence({"total": 1, "counts": {}}, policy, web, posts)
        self.assertIn("当前没有可联网检索的完整正文", combined["conclusion"])


if __name__ == "__main__":
    unittest.main()
