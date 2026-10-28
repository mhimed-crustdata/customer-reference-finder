import os
import sys
import tempfile
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reference_finder as rf  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

SINCE = "2026-09-01"


def post(**over):
    base = {
        "share_url": "https://example.com/post/1",
        "date_posted": "2026-09-20T10:00:00Z",
        "post_type": "original",
        "text": "We love Slack. It saved us hours every week.",
        "actor": {"actor_type": "person", "title": "VP of Operations", "company_name": "Acme Co"},
        "engagement": {"total_reactions": 25},
    }
    base.update(over)
    return base


def q(p, own="Slack"):
    return rf.qualify(p, "Slack", own, SINCE)


class QualifyTests(unittest.TestCase):
    def test_happy_customer_passes(self):
        rec = q(post())
        self.assertEqual(rec["company"], "Acme Co")
        self.assertEqual(rec["reactions"], 25)
        self.assertEqual(rec["posted"], "2026-09-20")

    def test_reposts_rejected(self):
        self.assertIsNone(q(post(post_type="repost_without_thoughts")))

    def test_company_pages_rejected(self):
        p = post()
        p["actor"]["actor_type"] = "company"
        self.assertIsNone(q(p))

    def test_old_posts_rejected(self):
        self.assertIsNone(q(post(date_posted="2026-08-01T00:00:00Z")))

    def test_own_employees_rejected(self):
        p = post()
        p["actor"]["company_name"] = "Slack Technologies"
        self.assertIsNone(q(p))

    def test_missing_title_or_company_rejected(self):
        p = post()
        p["actor"]["title"] = None
        self.assertIsNone(q(p))
        p = post()
        p["actor"]["company_name"] = ""
        self.assertIsNone(q(p))

    def test_product_must_be_a_whole_word(self):
        self.assertIsNone(q(post(text="We love slackers and great teams.")))

    def test_complaints_rejected(self):
        self.assertIsNone(q(post(text="We love Slack but the outage was awful.")))

    def test_needs_a_positive_cue(self):
        self.assertIsNone(q(post(text="We use Slack for team chat.")))

    def test_needs_a_senior_title(self):
        p = post()
        p["actor"]["title"] = "Intern"
        self.assertIsNone(q(p))


class ScoreTests(unittest.TestCase):
    def test_industry_and_size_match_rank_first(self):
        best = {"industry": "Software Development", "size": "201-500", "title": "CEO", "reactions": 50}
        other = {"industry": "Plastics Manufacturing", "size": "10001+", "title": "Manager", "reactions": 1}
        self.assertEqual(rf.score(best, "software development", "201-500"), 7)
        self.assertEqual(rf.score(other, "software development", "201-500"), 0)

    def test_adjacent_size_band_scores_one(self):
        rec = {"industry": "", "size": "51-200", "title": "Manager", "reactions": 0}
        self.assertEqual(rf.score(rec, "", "201-500"), 1)


class MergeTests(unittest.TestCase):
    def test_buckets_of_one_are_merged(self):
        out = rf.merge_small(Counter({"51-200": 3, "11-50": 1, "10001+": 1}))
        self.assertEqual(out, {"51-200": 3, "other": 2})


def make_transport(calls):
    def transport(url, body, headers, timeout):
        calls.append((url, body, headers))
        if url.endswith("/social_post/search"):
            return {"posts": [
                post(),
                post(share_url="https://example.com/post/2", post_type="repost_without_thoughts"),
                post(share_url="https://example.com/post/3",
                     actor={"actor_type": "person", "title": "Founder", "company_name": "Other Inc"},
                     engagement={"total_reactions": 3}),
            ], "next_cursor": None}
        if url.endswith("/company/enrich"):
            return [
                {"matched_on": "Acme Co", "matches": [
                    {"company_data": {"basic_info": {"name": "Acme Holdings", "employee_count_range": "10001+"}}},
                    {"company_data": {"basic_info": {"name": "Acme Co", "employee_count_range": "201-500"},
                                      "taxonomy": {"professional_network_industry": "Software Development"}}}]},
                {"matched_on": "Other Inc", "matches": [
                    {"company_data": {"basic_info": {"name": "Other Industries", "employee_count_range": "2-10"}}}]},
            ]
        raise AssertionError(url)
    return transport


class RunTests(unittest.TestCase):
    def test_run_uses_documented_endpoints_and_ranks(self):
        calls = []
        result = rf.run(Crustdata(api_key="test", transport=make_transport(calls)), "Slack", "Slack",
                        SINCE, 50, "Software Development", "201-500")
        self.assertTrue(calls[0][0].endswith("/social_post/search"))
        self.assertTrue(calls[1][0].endswith("/company/enrich"))
        self.assertEqual(calls[0][2]["x-api-version"], "2025-11-01")
        self.assertEqual(calls[1][1]["exact_match"], True)
        self.assertEqual(result["scanned"], 3)
        rows = result["rows"]
        self.assertEqual(len(rows), 2)  # the repost is dropped
        self.assertEqual(rows[0]["company"], "Acme Co")
        self.assertEqual(rows[0]["industry"], "Software Development")
        self.assertEqual(rows[0]["size"], "201-500")
        # fuzzy match on a different company name is not trusted
        self.assertEqual(rows[1]["industry"], "")
        self.assertEqual(rows[1]["size"], "")

    def test_filters_use_documented_fields(self):
        f = rf.build_filters("Slack", SINCE)
        fields = {c["field"]: c for c in f["conditions"]}
        self.assertEqual(fields["text"]["type"], "[.]")
        self.assertEqual(fields["post_type"]["value"], "original")
        self.assertEqual(fields["date_posted"]["type"], "=>")
        self.assertEqual(fields["actor.actor_type"]["value"], "person")

    def test_enrichment_is_capped(self):
        calls = []
        rf.run(Crustdata(api_key="test", transport=make_transport(calls)), "Slack", "Slack",
               SINCE, 50, max_companies=1)
        self.assertEqual(calls[1][1]["names"], ["Acme Co"])


class OutputTests(unittest.TestCase):
    def setUp(self):
        calls = []
        self.result = rf.run(Crustdata(api_key="test", transport=make_transport(calls)), "Slack", "Slack",
                             SINCE, 50, "Software Development", "201-500")

    def test_summary_is_aggregate_only(self):
        s = rf.summary(self.result["scanned"], self.result["rows"], "Software Development", "201-500")
        self.assertEqual(s["posts_scanned"], 3)
        self.assertEqual(s["qualified_references"], 2)
        self.assertEqual(s["enriched_with_company"], 1)
        self.assertEqual(s["same_industry_as_prospect"], 1)
        self.assertNotIn("Acme", str(s))

    def test_slack_text_has_ask_template_without_claude(self):
        text = rf.slack_text(self.result["rows"], "Slack", "Globex")
        self.assertIn("2 customers posted about Slack", text)
        self.assertIn("Software Development", text)
        self.assertIn("20 minute reference call", text)
        self.assertIn("Globex", text)

    def test_state_alerts_each_post_once(self):
        rows = self.result["rows"]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            self.assertEqual(len(rf.only_new(rows, path)), 2)
            rf.save_state(rows[:1], path)
            self.assertEqual(len(rf.only_new(rows, path)), 1)


if __name__ == "__main__":
    unittest.main()
