import math
import unittest

from compliance import CRITERION_IDS, DEFAULT_WEIGHTS, assess_compliance, unknown_content_review, validate_weights


def post(pid, **extra):
    return {"id": str(pid), "text": "可复核内容观点与观察过程", "type": "original", **extra}


def entry(pid, original=None, topic=None, status="completed"):
    review = unknown_content_review(status)
    review["model"] = "fixture-model"
    for row, score in ((review["criteria"][0], original), (review["criteria"][2], topic)):
        row["score"] = score
        row["verdict"] = "unknown" if score is None else "concern" if score < 40 else "mixed" if score < 70 else "supported"
    return {"id": str(pid), "content_review": review}


class ComplianceTests(unittest.TestCase):
    def test_all_unknown_is_not_zero_or_positive(self):
        result = assess_compliance([post(1), post(2), post(3, type="repost")])
        self.assertIsNone(result["score"])
        self.assertEqual((0, 0, 100), tuple(result[key] for key in ("coverage_percent", "supported_percent", "unknown_weight")))
        self.assertEqual({"low": 0, "high": 100}, result["evidence_range"])
        self.assertTrue(all(row["score"] is None and row["unknown_count"] == 2 for row in result["criteria"]))

    def test_weighted_formula_and_partial_evidence_range(self):
        posts = [post(i) for i in range(10)]
        result = assess_compliance(posts, {"posts": [entry(0, 80, 100)]})
        self.assertEqual((83.33, 6, 5, 94), tuple(result[key] for key in ("score", "coverage_percent", "supported_percent", "unknown_weight")))
        self.assertEqual({"low": 5, "high": 99}, result["evidence_range"])
        self.assertEqual((80, 1, 9, 10), tuple(result["criteria"][0][key] for key in ("score", "evaluated_count", "unknown_count", "coverage_percent")))

    def test_unknown_addition_preserves_assessed_score_and_widens_bounds(self):
        evidence = {"posts": [entry(0, 80, 100)]}
        full = assess_compliance([post(0)], evidence)
        partial = assess_compliance([post(0), post(1)], evidence)
        self.assertEqual(full["score"], partial["score"])
        self.assertGreater(full["coverage_percent"], partial["coverage_percent"])
        self.assertGreater(full["evidence_range"]["low"], partial["evidence_range"]["low"])
        self.assertLess(full["evidence_range"]["high"], partial["evidence_range"]["high"])

    def test_replicating_identical_judgments_gives_no_quantity_bonus(self):
        single = assess_compliance([post(0)], {"posts": [entry(0, 50, 70)]})
        repeated = assess_compliance([post(i) for i in range(20)], {"posts": [entry(i, 50, 70) for i in range(20)]})
        for key in ("score", "coverage_percent", "supported_percent", "unknown_weight", "evidence_range"):
            self.assertEqual(single[key], repeated[key])
        self.assertEqual(20, repeated["criteria"][0]["evidence_total"])
        self.assertEqual(4, len(repeated["criteria"][0]["evidence"]))

    def test_selected_scope_does_not_extrapolate_or_accept_other_post_evidence(self):
        posts = [post(i) for i in range(30)]
        result = assess_compliance(posts, {"posts": [entry(0, 0, 0), entry(29, 100, 100)]}, selected_ids=[str(i) for i in range(10)])
        self.assertEqual(0, result["score"])
        self.assertEqual(10, result["evidence_scope"]["scope_count"])
        self.assertEqual("not_estimated", result["evidence_scope"]["projection"])
        self.assertEqual(1, result["criteria"][0]["evaluated_count"])
        for ids in (["0", "0"], ["missing"]):
            with self.assertRaises(ValueError):
                assess_compliance(posts, selected_ids=ids)

    def test_actual_zero_and_hundred_remain_distinct_from_unknown(self):
        for score in (0, 100):
            with self.subTest(score=score):
                result = assess_compliance([post(0)], {"posts": [entry(0, score, score)]})
                self.assertEqual(score, result["score"])
                self.assertEqual(60, result["coverage_percent"])
                self.assertEqual(40, result["unknown_weight"])
        unknown = assess_compliance([post(0)], {"posts": [entry(0, None, 100)]})
        self.assertIsNone(unknown["score"])
        self.assertEqual(10, unknown["coverage_percent"])

    def test_automation_ip_and_noncompleted_reviews_cannot_supply_scores(self):
        reviewed = entry(0, 100, 100)
        for row in reviewed["content_review"]["criteria"]:
            row["score"] = 100
        result = assess_compliance([post(0)], {"posts": [reviewed]})
        self.assertTrue(all(result["criteria"][index]["score"] is None for index in (1, 3)))
        for status in ("failed", "not_configured"):
            result = assess_compliance([post(0)], {"posts": [entry(0, 100, 100, status)]})
            self.assertIsNone(result["score"])
            self.assertEqual(0, result["coverage_percent"])

    def test_weights_are_fixed_numeric_finite_and_original_has_positive_weight(self):
        self.assertEqual(DEFAULT_WEIGHTS, validate_weights(dict(DEFAULT_WEIGHTS)))
        invalid = [None, {}, {**DEFAULT_WEIGHTS, "extra": 0}, {**DEFAULT_WEIGHTS, "automation": True},
                   {**DEFAULT_WEIGHTS, "automation": "20"}, {**DEFAULT_WEIGHTS, "automation": math.nan},
                   {**DEFAULT_WEIGHTS, "automation": math.inf}, {**DEFAULT_WEIGHTS, "automation": -1},
                   {**DEFAULT_WEIGHTS, "automation": 101}, {**DEFAULT_WEIGHTS, "automation": 21},
                   dict(zip(CRITERION_IDS, (0, 0, 100, 0)))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_weights(value)

    def test_changing_weights_changes_aggregate_and_preserves_judgments(self):
        posts = [post(0)]
        web = {"posts": [entry(0, 0, 100)]}
        before = assess_compliance(posts, web)
        after = assess_compliance(posts, web, weights=dict(zip(CRITERION_IDS, (10, 20, 50, 20))))
        self.assertEqual((16.67, 83.33), (before["score"], after["score"]))
        self.assertEqual(before["evidence_scope"], after["evidence_scope"])
        for previous, current in zip(before["criteria"], after["criteria"]):
            for key in ("score", "evidence", "evaluated_count", "unknown_count", "denominator"):
                self.assertEqual(previous[key], current[key])


if __name__ == "__main__":
    unittest.main()
