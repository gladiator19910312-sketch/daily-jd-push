import json
import unittest
import urllib.parse
from unittest.mock import patch

from radar_ats import (
    ClosedJobError,
    microsoft_is_mainland,
    parse_amazon,
    parse_microsoft_detail,
    parse_microsoft_search,
)
from radar_discovery import discover_jobs, discover_jobs_with_coverage, fetch_official_source
from radar_types import Job


def amazon_payload() -> bytes:
    substantive = (
        "Own an agentic AI product roadmap, define evaluation benchmarks, trace failures, "
        "and partner with engineering to launch reliable customer-facing workflows."
    )
    return json.dumps(
        {
            "hits": 5,
            "jobs": [
                {
                    "id_icims": "3114650",
                    "title": "Senior AI Product Manager, Agentic AI",
                    "job_path": "/en/jobs/3114650/senior-ai-product-manager",
                    "country_code": "CHN",
                    "city": "Beijing",
                    "location": "CHN, Beijing",
                    "locations": [
                        json.dumps(
                            {
                                "normalizedCountryCode": "CHN",
                                "normalizedLocation": "Beijing, CHN",
                            }
                        )
                    ],
                    "posted_date": "July 3, 2026",
                    "description": substantive,
                    "basic_qualifications": "7+ years of product management experience.",
                    "preferred_qualifications": "Experience with AI evaluations and safety.",
                    "university_job": False,
                },
                {
                    "id_icims": "us-role",
                    "title": "Senior AI Product Manager",
                    "job_path": "/en/jobs/3000001/senior-ai-product-manager",
                    "country_code": "USA",
                    "description": substantive,
                },
                {
                    "id_icims": "closed-role",
                    "title": "Agent Product Lead",
                    "job_path": "/en/jobs/3000002/agent-product-lead",
                    "country_code": "CHN",
                    "status": "closed",
                    "description": substantive,
                },
                {
                    "id_icims": "external-role",
                    "title": "Agent Product Lead",
                    "url": "https://evil.example/jobs/3000003",
                    "country_code": "CHN",
                    "description": substantive,
                },
                {
                    "id_icims": "campus-role",
                    "title": "AI Product Manager",
                    "job_path": "/en/jobs/3000004/ai-product-manager",
                    "country_code": "CHN",
                    "university_job": True,
                    "description": substantive,
                },
            ],
        }
    ).encode()


class AmazonOfficialSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "type": "amazon",
            "key": "amazon:china-social",
            "name": "Amazon 中国官方社招",
            "company": "Amazon",
            "scope": "china",
            "url": "https://www.amazon.jobs/en/search.json",
            "loc_query": "China",
            "keywords": ["AI Product Manager"],
            "page_size": 20,
            "max_pages_per_keyword": 1,
        }

    def test_parse_amazon_keeps_only_live_mainland_social_role(self):
        jobs, total = parse_amazon(amazon_payload(), self.source)

        self.assertEqual(total, 5)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "3114650")
        self.assertEqual(job.company, "Amazon")
        self.assertEqual(job.location, "Beijing, CHN")
        self.assertEqual(job.published_at, "2026-07-03T00:00:00+00:00")
        self.assertEqual(job.date_basis, "published")
        self.assertTrue(job.active)
        self.assertTrue(job.official)
        self.assertEqual(
            job.url,
            "https://www.amazon.jobs/en/jobs/3114650/senior-ai-product-manager",
        )
        self.assertIn("evaluation benchmarks", job.summary)
        self.assertIn("7+ years", job.summary)

    @patch("radar_discovery.http_get", return_value=amazon_payload())
    def test_fetch_amazon_builds_public_search_query(self, mock_get):
        jobs = fetch_official_source(self.source)

        self.assertEqual([job.job_id for job in jobs], ["3114650"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(mock_get.call_args.args[0]).query)
        self.assertEqual(query["base_query"], ["AI Product Manager"])
        self.assertEqual(query["loc_query"], ["China"])
        self.assertEqual(query["offset"], ["0"])
        self.assertEqual(query["result_limit"], ["20"])
        self.assertEqual(query["sort"], ["relevant"])

    @patch("radar_discovery.http_get")
    def test_fetch_amazon_marks_truncated_keyword_as_partial(self, mock_get):
        payload = json.loads(amazon_payload())
        payload["hits"] = 25
        mock_get.return_value = json.dumps(payload).encode()
        warnings = []
        fetch_official_source(self.source, warnings=warnings)

        self.assertTrue(any("分页上限" in warning for warning in warnings))

    def test_amazon_rejects_truthy_intern_and_explicit_non_full_time(self):
        payload = json.loads(amazon_payload())
        base = payload["jobs"][0]
        intern = dict(base, id_icims="intern", is_intern="true")
        part_time = dict(base, id_icims="part-time", job_schedule_type="Part-Time")
        payload["jobs"] = [intern, part_time]

        jobs, _ = parse_amazon(json.dumps(payload).encode(), self.source)

        self.assertEqual(jobs, [])


def microsoft_search_payload() -> bytes:
    return json.dumps(
        {
            "status": 200,
            "error": {"message": "", "body": ""},
            "data": {
                "positions": [
                    {
                        "id": 1970393556939001,
                        "displayJobId": "200044001",
                        "name": "Principal Product Manager, Copilot Agent Evaluation",
                        "locations": ["China, Beijing, Beijing"],
                        "standardizedLocations": ["Beijing, Beijing, CN"],
                        "postedTs": 1784298852,
                        "department": "Product Management",
                        "workLocationOption": "onsite",
                        "positionUrl": "/careers/job/1970393556939001",
                    },
                    {
                        "id": 1970393556939002,
                        "name": "Software Engineer II - Voice Agent",
                        "locations": ["China, Beijing, Beijing"],
                        "standardizedLocations": ["Beijing, Beijing, CN"],
                        "postedTs": 1784298852,
                        "department": "Software Engineering",
                        "positionUrl": "/careers/job/1970393556939002",
                    },
                ],
                "count": 2,
            },
        }
    ).encode()


def microsoft_detail_payload() -> bytes:
    description = (
        "Own the Copilot agent evaluation roadmap and define real-world benchmarks, "
        "failure attribution, reliability gates, safety tradeoffs, and measurable product outcomes."
    )
    return json.dumps(
        {
            "status": 200,
            "error": {"message": "", "body": ""},
            "data": {
                "id": 1970393556939001,
                "displayJobId": "200044001",
                "name": "Principal Product Manager, Copilot Agent Evaluation",
                "locations": ["China, Beijing, Beijing"],
                "standardizedLocations": ["Beijing, Beijing, CN"],
                "postedTs": 1784298852,
                "jobDescription": f"<p>{description}</p>",
                "publicUrl": "https://apply.careers.microsoft.com/careers/job/1970393556939001",
                "efcustomTextWorkSite": ["Fully on-site"],
                "efcustomTextRequiredTravel": ["Less than 25%"],
                "efcustomTextCurrentProfession": ["Product Management"],
                "efcustomTextRoletype": ["Individual Contributor"],
                "efcustomTextEmploymentType": ["Full-Time"],
                "positionUserActions": {"applyAction": {"status": "log_in"}},
            },
        }
    ).encode()


class MicrosoftOfficialSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "type": "microsoft",
            "key": "microsoft:beijing-social",
            "name": "Microsoft 北京官方社招",
            "company": "Microsoft",
            "scope": "china",
            "url": "https://apply.careers.microsoft.com/api/pcsx/search",
            "detail_url": "https://apply.careers.microsoft.com/api/pcsx/position_details",
            "domain": "microsoft.com",
            "location": "China, Beijing",
            "keywords": ["AI Product Manager"],
            "page_size": 10,
            "max_pages_per_keyword": 2,
            "max_detail_fetches": 12,
            "sort_by": "timestamp",
            "language": "en",
        }

    def test_search_is_discovery_only_and_filters_non_product_roles(self):
        jobs, total = parse_microsoft_search(microsoft_search_payload(), self.source)

        self.assertEqual(total, 2)
        self.assertEqual([job.job_id for job in jobs], ["1970393556939001"])
        self.assertFalse(jobs[0].active)
        self.assertEqual(jobs[0].location, "China, Beijing, Beijing")
        self.assertEqual(
            jobs[0].url,
            "https://apply.careers.microsoft.com/careers/job/1970393556939001",
        )

    def test_mainland_location_accepts_country_last_variants(self):
        self.assertTrue(microsoft_is_mainland({"locations": ["Beijing, Beijing, China"]}))
        self.assertTrue(microsoft_is_mainland({"location": "Multiple Locations, China"}))
        self.assertFalse(microsoft_is_mainland({"location": "Hong Kong, China"}))

    def test_detail_confirms_full_active_jd_and_preserves_travel(self):
        job = parse_microsoft_detail(
            microsoft_detail_payload(),
            self.source,
            "1970393556939001",
        )

        self.assertTrue(job.active)
        self.assertTrue(job.official)
        self.assertEqual(job.company, "Microsoft")
        self.assertIn("failure attribution", job.summary)
        self.assertIn("Travel: Less than 25%", job.summary)
        self.assertIn("Role type: Individual Contributor", job.summary)

    def test_detail_rejects_missing_or_unknown_apply_status(self):
        payload = json.loads(microsoft_detail_payload())
        payload["data"]["positionUserActions"] = {}
        with self.assertRaises(ClosedJobError):
            parse_microsoft_detail(json.dumps(payload).encode(), self.source, "1970393556939001")

        payload["data"]["positionUserActions"] = {
            "applyAction": {"status": "mystery"}
        }
        with self.assertRaises(ClosedJobError):
            parse_microsoft_detail(json.dumps(payload).encode(), self.source, "1970393556939001")

    @patch("radar_discovery.http_get")
    def test_fetch_microsoft_requires_detail_success(self, mock_get):
        mock_get.side_effect = [microsoft_search_payload(), microsoft_detail_payload()]

        jobs = fetch_official_source(self.source)

        self.assertEqual([job.job_id for job in jobs], ["1970393556939001"])
        self.assertEqual(mock_get.call_count, 2)
        search_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(mock_get.call_args_list[0].args[0]).query
        )
        detail_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(mock_get.call_args_list[1].args[0]).query
        )
        self.assertEqual(search_query["location"], ["China, Beijing"])
        self.assertEqual(search_query["start"], ["0"])
        self.assertEqual(detail_query["position_id"], ["1970393556939001"])

    @patch("radar_discovery.http_get")
    def test_fetch_microsoft_never_falls_back_to_thin_search_result(self, mock_get):
        mock_get.side_effect = [microsoft_search_payload(), b"not-json"]
        warnings = []

        jobs = fetch_official_source(self.source, warnings=warnings)

        self.assertEqual(jobs, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("详情验活失败", warnings[0])


class OfficialCoverageTests(unittest.TestCase):
    @patch("radar_discovery.fetch_official_source")
    def test_discovery_reports_each_source_without_leaking_exception_text(self, mock_fetch):
        job = Job(
            "Agent 评测产品经理",
            "https://www.amazon.jobs/en/jobs/3114650/agent-product-manager",
            "负责 Agent 评测、Benchmark 与产品闭环。",
            "甲公司官方",
            "3114650",
            "北京",
            "china",
            True,
            "official:a",
        )

        def fetch(source, _budget, warnings):
            if source["key"] == "official:a":
                warnings.append("甲公司官方: 部分关键词请求失败，已保留其余结果")
                return [job]
            raise ValueError("https://example.com/?private=must-not-leak")

        mock_fetch.side_effect = fetch
        config = {
            "official_sources": [
                {"type": "test", "key": "official:a", "name": "甲公司官方", "scope": "china"},
                {"type": "test", "key": "official:b", "name": "乙公司官方", "scope": "global"},
            ],
            "queries": [],
        }

        jobs, failures, coverage = discover_jobs_with_coverage(config)

        self.assertEqual(jobs, [job])
        self.assertEqual([item.status for item in coverage], ["partial", "error"])
        self.assertEqual([item.count for item in coverage], [1, 0])
        self.assertEqual(coverage[1].diagnostic, "ValueError")
        self.assertNotIn("must-not-leak", json.dumps([item.__dict__ for item in coverage]))
        self.assertTrue(any("企业官网成功 0/2" in failure for failure in failures))

    @patch("radar_discovery.fetch_official_source", return_value=[])
    def test_legacy_discover_jobs_return_shape_is_unchanged(self, _mock_fetch):
        result = discover_jobs(
            {
                "official_sources": [
                    {"type": "test", "key": "official:a", "name": "甲公司官方"}
                ],
                "queries": [],
            }
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result, ([], []))

        _, _, coverage = discover_jobs_with_coverage(
            {
                "official_sources": [
                    {"type": "test", "key": "official:a", "name": "甲公司官方"}
                ],
                "queries": [],
            }
        )
        self.assertEqual(coverage[0].diagnostic, "扫描成功，未发现目标岗位")

    @patch("radar_discovery.time.monotonic", side_effect=[10.0, 11.0, 11.0])
    def test_budget_exhaustion_marks_every_remaining_source_skipped(self, _mock_clock):
        _, failures, coverage = discover_jobs_with_coverage(
            {
                "official_discovery_budget_seconds": 0,
                "official_sources": [
                    {"type": "test", "key": "official:a", "name": "甲公司官方"},
                    {"type": "test", "key": "official:b", "name": "乙公司官方"},
                ],
                "queries": [],
            }
        )

        self.assertEqual([item.source_key for item in coverage], ["official:a", "official:b"])
        self.assertEqual([item.status for item in coverage], ["skipped", "skipped"])
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
