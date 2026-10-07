import json
import unittest
from unittest.mock import patch

from archive_adapter import prepare_archive, finish_archive
from engine import analyze, MAX_RELATED_EVIDENCE
from importers import ImportErrorDetail, normalize_project


TEXT = "我在自己的设备上记录实际测试过程，比较不同条件下的加载延迟和错误，并给出可复核的观察与结论。这些记录说明了结果的使用范围和局限。"


def file(records, name="data/tweets.js", part=0):
    return {"name": name, "text": f"window.YTD.tweets.part{part} = " + json.dumps([{"tweet": r} for r in records], ensure_ascii=False) + ";"}


def tweet(identity, text=TEXT, **fields):
    return {"id_str": str(identity), "full_text": text, "created_at": "Wed Oct 07 00:00:00 +0000 2026", **fields}


def independent_records(count=40):
    # Each long body has disjoint character grams, so these fixtures test the
    # probability rule without relying on one shared writing template.
    return [tweet(1000000000000000000 + i, chr(0x5000 + i) * 60) for i in range(count)]


def estimate(records, metadata=None):
    return finish_archive(prepare_archive([file(records)], metadata))


class ArchiveAdapterTests(unittest.TestCase):
    def test_subjective_estimate_distinguishes_rich_material_from_repetitive_engagement(self):
        clean = estimate(independent_records())
        repetitive = estimate([tweet(1000000000000000000 + i, TEXT + "关注并转发") for i in range(40)])
        self.assertEqual(75, clean["summary"]["estimated_probability"])
        self.assertEqual("中", clean["summary"]["probability_confidence"])
        self.assertEqual(5, repetitive["summary"]["estimated_probability"])
        self.assertEqual("低", repetitive["summary"]["probability_confidence"])
        self.assertGreater(clean["summary"]["estimated_probability"], repetitive["summary"]["estimated_probability"])
        self.assertTrue(any(f["code"] == "engagement_template" and f["impact_points"] < 0 for f in repetitive["probability_factors"]))
        self.assertIn("并不等于抄袭", next(f["detail"] for f in repetitive["probability_factors"] if f["code"] == "exact_repetition"))

    def test_probability_is_not_just_nonduplicate_percentage_and_reposts_reduce_support(self):
        single = estimate(independent_records(1))
        rich = estimate(independent_records(40))
        dominated = estimate(independent_records(40) + [tweet(2000000000000000000 + i, "RT @source: " + TEXT) for i in range(160)])
        self.assertEqual(100, single["summary"]["nonduplicate_percent"])
        self.assertEqual(100, rich["summary"]["nonduplicate_percent"])
        self.assertLess(single["summary"]["estimated_probability"], rich["summary"]["estimated_probability"])
        self.assertEqual(65, dominated["summary"]["estimated_probability"])
        self.assertEqual("低", dominated["summary"]["probability_confidence"])
        self.assertIn("不等于违规", next(f["detail"] for f in dominated["probability_factors"] if f["code"] == "repost_dominated"))

    def test_missing_comparable_body_declines_numeric_guess_and_partial_material_stays_low(self):
        empty = estimate([tweet("100000001", ""), tweet("100000002", "短句"), tweet("100000003", "RT @source: " + TEXT)])
        self.assertIsNone(empty["summary"]["estimated_probability"])
        self.assertEqual({"low": None, "high": None}, empty["summary"]["probability_range"])
        self.assertIn("无法给出有意义", empty["summary"]["probability_explanation"])
        sparse = estimate(independent_records(2) + [tweet(3000000000000000000 + i, "", truncated=True) for i in range(98)])
        self.assertEqual(10, sparse["summary"]["estimated_probability"])
        self.assertEqual("低", sparse["summary"]["probability_confidence"])
        self.assertEqual(98, sparse["summary"]["comparison_excluded_posts"])

    def test_probability_comparison_budget_widens_uncertainty_and_never_raises_confidence(self):
        records = independent_records(40)
        complete = estimate(records)
        with patch("engine.MAX_NEAR_COMPARISONS", 0):
            limited = estimate(records)
        self.assertLess(limited["summary"]["estimated_probability"], complete["summary"]["estimated_probability"])
        self.assertEqual("低", limited["summary"]["probability_confidence"])
        normal_range = complete["summary"]["probability_range"]
        limited_range = limited["summary"]["probability_range"]
        self.assertGreater(limited_range["high"] - limited_range["low"], normal_range["high"] - normal_range["low"])

    def test_duplicate_count_does_not_grow_distinct_bonus_or_confidence(self):
        distinct = independent_records(20)
        original = estimate(distinct)
        duplicated = [tweet(4000000000000000000 + i, distinct[i % 20]["full_text"]) for i in range(200)]
        repeated = estimate(duplicated)
        for result in (original, repeated):
            self.assertEqual(10, next(f["impact_points"] for f in result["probability_factors"] if f["code"] == "distinct_body_support"))
            self.assertEqual("低", result["summary"]["probability_confidence"])
        self.assertLess(repeated["summary"]["estimated_probability"], original["summary"]["estimated_probability"])

    def test_probability_media_hash_does_not_prove_authorship_and_external_notes_lower_confidence(self):
        records = independent_records(40)
        for record in records:
            record["extended_entities"] = {"media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/common.jpg"}]}
        media_result = {"hashes": [{"name": f"{record['id_str']}-common.jpg", "hash": "a" * 64} for record in records]}
        no_media = estimate(independent_records(40))
        hashed = finish_archive(prepare_archive([file(records)]), media_result)
        self.assertLess(hashed["summary"]["estimated_probability"], no_media["summary"]["estimated_probability"])
        self.assertEqual("低", hashed["summary"]["probability_confidence"])
        self.assertTrue(any(f["code"] == "media_uninterpreted" for f in hashed["probability_factors"]))
        notes = estimate(independent_records(40), {"unread_note_files": 1})
        self.assertEqual("低", notes["summary"]["probability_confidence"])
        self.assertTrue(any(f["code"] == "unread_long_text" for f in notes["probability_factors"]))

    def test_probability_boundaries_factor_sum_range_contains_center_and_json_contract(self):
        cases = [independent_records(1), independent_records(9), independent_records(100),
                 [tweet(5000000000000000000 + i, TEXT + "关注并转发") for i in range(100)]]
        for records in cases:
            result = estimate(records)
            summary = result["summary"]
            center = summary["estimated_probability"]
            self.assertGreaterEqual(center, 5)
            self.assertLessEqual(center, 85)
            self.assertEqual(0, center % 5)
            self.assertEqual(center, sum(f["impact_points"] for f in result["probability_factors"]))
            self.assertLessEqual(summary["probability_range"]["low"], center)
            self.assertGreaterEqual(summary["probability_range"]["high"], center)
            self.assertGreaterEqual(summary["probability_range"]["low"], 0)
            self.assertLessEqual(summary["probability_range"]["high"], 100)
            self.assertIn(summary["probability_confidence"], {"低", "中"})
            self.assertEqual({"version": "subjective-rules-1.0", "calibrated": False, "method": "heuristic"}, summary["probability_model"])
            self.assertIsNone(summary["official_probability"])
            self.assertEqual(result, json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False)))

    def test_more_than_2000_posts_all_analyzed_across_parts(self):
        records = [tweet(1000000000000000000 + i, f"第 {i} 条短记录") for i in range(2601)]
        prepared = prepare_archive([file(records[:2000]), file(records[2000:], "data/tweets-part1.js", 1)], {"input_bytes": 300 * 1024 * 1024, "zip_entries": 12000})
        result = finish_archive(prepared)
        self.assertEqual(2601, result["summary"]["total"])
        self.assertEqual(2601, result["coverage"]["archive_records"])
        self.assertEqual(2, result["coverage"]["post_files"])
        self.assertTrue(result["summary"]["analyzed_all_archive_posts"])
        self.assertEqual(2601, result["summary"]["comparison_excluded_posts"])
        self.assertIsNone(result["summary"]["nonduplicate_percent"])
        with self.assertRaises(ImportErrorDetail):
            normalize_project({"posts": records})  # Legacy material limits still apply.

    def test_duplicate_groups_evidence_is_bounded_and_total_is_preserved(self):
        records = [tweet(1000000000000000000 + i) for i in range(5001)]
        prepared = prepare_archive([file(records)])
        result = analyze(prepared["project"], full_archive=True, compact=True)
        self.assertEqual(5001, result["summary"]["counts"]["high_risk"])
        self.assertEqual(0, result["coverage"]["near_comparisons"])
        for post in result["posts"]:
            evidence = next(r["evidence"] for r in post["reasons"] if r["code"] == "internal_exact")
            self.assertEqual(5000, evidence["related_posts_total"])
            self.assertLessEqual(len(evidence["related_posts"]), MAX_RELATED_EVIDENCE)
        summary = finish_archive(prepared)
        self.assertEqual(0.0, summary["summary"]["nonduplicate_percent"])
        self.assertLessEqual(len(summary["examples"]), 12)
        self.assertLess(len(json.dumps(summary)), 30000)

    def test_types_actual_parent_threads_and_denom_do_not_claim_originality(self):
        records = [tweet("100000001", TEXT), tweet("100000002", TEXT + "这是补充细节", in_reply_to_status_id_str="100000001"),
                   tweet("100000003", "RT @source: " + TEXT), tweet("100000004", "引用的新增观点" + TEXT, is_quote_status=True),
                   tweet("100000005", "https://t.co/only"), tweet("100000006", "hi"),
                   tweet("100000007", TEXT + "不完整", truncated=True)]
        prepared = prepare_archive([file(records)])
        result = finish_archive(prepared)
        self.assertEqual({"posts": 4, "reply": 1, "repost": 1, "quote": 1}, result["summary"]["types"])
        self.assertEqual(1, result["summary"]["thread_groups"])
        self.assertEqual("100000001", prepared["project"]["posts"][1]["reply_to"])
        self.assertEqual(3, result["summary"]["nonduplicate_denominator"])
        self.assertEqual(3, result["summary"]["comparison_excluded_posts"])
        self.assertIsNone(result["summary"]["official_probability"])
        self.assertIn("不能证明线程", " ".join(result["coverage"]["notes"]))

    def test_media_variants_matched_by_archive_filename_without_private_files(self):
        photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/picture.jpg"}
        video = {"type": "video", "media_url_https": "https://pbs.twimg.com/media/preview.jpg", "video_info": {"variants": [
            {"content_type": "video/mp4", "url": "https://video.twimg.com/video/clip.mp4?tag=1"},
            {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/video/master.m3u8"}]}}
        prepared = prepare_archive([file([tweet("100000001", extended_entities={"media": [photo, video]}, direct_messages="PRIVATE MUST NEVER LEAK")])])
        self.assertEqual(["100000001-picture.jpg", "100000001-clip.mp4"], prepared["media_names"])
        result = finish_archive(prepared, {"hashes": [{"name": "100000001-picture.jpg", "hash": "a" * 64}, {"name": "100000001-clip.mp4", "hash": "b" * 64}]})
        self.assertEqual(2, result["summary"]["hashed_media"])
        self.assertEqual(100, result["summary"]["media_completeness_percent"])
        self.assertNotIn("PRIVATE MUST NEVER LEAK", json.dumps(result))
        self.assertTrue(any(r["code"] == "media_content_unchecked" for r in result["reasons"]))

    def test_embedded_long_text_restored_but_article_and_external_notes_not_claimed_complete(self):
        prepared = prepare_archive([file([tweet("100000001", "短摘要", note_tweet={"note_tweet_results": {"result": {"text": TEXT}}}),
            tweet("100000002", "文章链接", entities={"urls": [{"expanded_url": "https://x.com/i/article/1"}]})])], {"unread_note_files": 1})
        self.assertEqual(TEXT, prepared["project"]["posts"][0]["text"])
        self.assertTrue(prepared["project"]["posts"][0]["text_complete"])
        self.assertFalse(prepared["project"]["posts"][1]["text_complete"])
        result = finish_archive(prepared)
        self.assertEqual(1, result["summary"]["incomplete_text_posts"])
        self.assertTrue(any("独立长帖" in w for w in result["warnings"]))

    def test_invalid_rows_private_paths_and_trailing_code_error_before_results(self):
        invalid = [file([None]), {"name": "data/account.js", "text": "[]"},
            {"name": "../tweets.js", "text": "[]"}, {"name": "data/tweets.js", "text": file([tweet("1")])["text"] + "alert('execute')"},
            file([{"full_text": "missing id"}])]
        for value in invalid:
            with self.subTest(value=value["name"]):
                with self.assertRaises(ImportErrorDetail):
                    prepare_archive([value])

    def test_literal_none_text_retained_and_overlapping_parts_reported(self):
        prepared = prepare_archive([file([tweet("1", "none"), tweet("2", "无")]), file([tweet("1", "none")], "data/tweets-part2.js", 2)])
        result = finish_archive(prepared)
        self.assertEqual(3, result["coverage"]["archive_records"])
        self.assertEqual(2, result["summary"]["total"])
        self.assertEqual("none", prepared["project"]["posts"][0]["text"])
        self.assertTrue(any("分片编号存在缺口" in w for w in result["warnings"]))
        self.assertTrue(any("分片重复记录合并" in w for w in result["warnings"]))

    def test_near_budget_limit_explicitly_warns_and_never_sets_probability(self):
        prepared = prepare_archive([file([tweet("100000001")])])
        with patch("engine.MAX_NEAR_COMPARISONS", 0):
            result = finish_archive(prepared)
        self.assertTrue(result["coverage"]["approximate_comparison_limited"])
        self.assertTrue(any(r["code"] == "comparison_budget" for r in result["reasons"]))
        self.assertIsNone(result["summary"]["official_probability"])


if __name__ == "__main__":
    unittest.main()
