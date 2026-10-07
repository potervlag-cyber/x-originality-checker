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
    # comparison scope without relying on one shared writing template.
    return [tweet(1000000000000000000 + i, chr(0x5000 + i) * 60) for i in range(count)]


def estimate(records, metadata=None):
    return finish_archive(prepare_archive([file(records)], metadata))


class ArchiveAdapterTests(unittest.TestCase):
    def test_quantity_and_repetition_cannot_invent_content_compliance(self):
        clean = estimate(independent_records(40))
        repetitive = estimate([tweet(1000000000000000000 + i, TEXT + "关注并转发") for i in range(40)])
        for result in (clean, repetitive):
            compliance = result["summary"]["compliance"]
            self.assertIsNone(compliance["score"])
            self.assertEqual(0, compliance["coverage_percent"])
            self.assertEqual({"low": 0, "high": 100}, compliance["evidence_range"])
            self.assertEqual(40, compliance["evidence_scope"]["scope_count"])
            self.assertTrue(all(row["score"] is None for row in compliance["criteria"]))
            self.assertNotIn("probability_factors", result)
            self.assertNotIn("estimated_probability", result["summary"])
        self.assertEqual(0, repetitive["summary"]["nonduplicate_percent"])
        self.assertEqual(100, clean["summary"]["nonduplicate_percent"])

    def test_local_only_short_incomplete_and_repost_material_stays_unknown(self):
        result = estimate([tweet("1", "短帖"), tweet("2", TEXT, truncated=True), tweet("3", "RT @source: " + TEXT)])
        compliance = result["summary"]["compliance"]
        self.assertIsNone(compliance["score"])
        self.assertEqual(2, compliance["evidence_scope"]["scope_count"])
        self.assertTrue(all(row["unknown_count"] == 2 for row in compliance["criteria"]))

    def test_comparison_budget_does_not_create_semantic_score(self):
        prepared = prepare_archive([file(independent_records(40))])
        with patch("engine.MAX_NEAR_COMPARISONS", 0):
            result = finish_archive(prepared)
        self.assertTrue(result["coverage"]["approximate_comparison_limited"])
        self.assertIsNone(result["summary"]["compliance"]["score"])

    def test_media_hash_and_long_note_metadata_cannot_create_semantic_score(self):
        photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/common.jpg"}
        records = independent_records(40)
        for record in records:
            record["extended_entities"] = {"media": [photo]}
        media_result = {"hashes": [{"name": f"{record['id_str']}-common.jpg", "hash": "a" * 64} for record in records]}
        result = finish_archive(prepare_archive([file(records)], {"unread_note_files": 1}), media_result)
        self.assertEqual(40, result["summary"]["hashed_media"])
        self.assertIsNone(result["summary"]["compliance"]["score"])
        self.assertEqual(100, result["summary"]["compliance"]["unknown_weight"])
        self.assertTrue(any("独立长帖" in warning for warning in result["warnings"]))
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
        self.assertIsNone(result["summary"]["compliance"]["score"])
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

    def test_near_budget_limit_explicitly_warns_and_never_invents_compliance(self):
        prepared = prepare_archive([file([tweet("100000001")])])
        with patch("engine.MAX_NEAR_COMPARISONS", 0):
            result = finish_archive(prepared)
        self.assertTrue(result["coverage"]["approximate_comparison_limited"])
        self.assertTrue(any(r["code"] == "comparison_budget" for r in result["reasons"]))
        self.assertIsNone(result["summary"]["compliance"]["score"])


if __name__ == "__main__":
    unittest.main()
