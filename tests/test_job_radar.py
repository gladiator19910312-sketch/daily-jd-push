import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from job_radar import (
    Job,
    assess_job,
    enrich_jobs,
    format_report,
    load_seen,
    looks_like_product_job,
    parse_ashby,
    parse_greenhouse,
    parse_moka,
    parse_rss,
    parse_salary,
    salary_gate,
    save_seen,
    select_for_push,
    signed_webhook_url,
    validate_dingtalk_webhook,
)


CONFIG = {
    "current_fixed_cash_wan": 100.0,
    "target_total_comp_wan": 140.0,
    "usd_cny": 7.0,
    "preferred_companies": ["Google", "DeepSeek", "京东"],
}


class JobRadarTests(unittest.TestCase):
    def test_parse_rss(self):
        payload = b"""<?xml version='1.0'?><rss><channel><item>
        <title>Agent Product Manager</title><link>https://example.com/jobs/1</link>
        <description><![CDATA[Own <b>agent evals</b> and safety.]]></description>
        </item></channel></rss>"""
        jobs = parse_rss(payload, "test", 10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].summary, "Own agent evals and safety.")

    def test_parse_chinese_monthly_salary(self):
        salary = parse_salary("AI座舱产品经理 50-80K·20薪")
        self.assertEqual(salary.total_high_wan, 160.0)
        self.assertEqual(salary.fixed_high_wan, 96.0)

    def test_parse_us_salary(self):
        salary = parse_salary("Base $163K - $237K + equity", usd_cny=7.0)
        self.assertEqual(salary.total_low_wan, 114.1)
        self.assertEqual(salary.total_high_wan, 165.9)

    def test_parse_full_usd_salary(self):
        salary = parse_salary("The base salary range is $204,000 - $259,000 per year", usd_cny=7.0)
        self.assertEqual(salary.total_low_wan, 142.8)
        self.assertEqual(salary.total_high_wan, 181.3)

    def test_fixed_cash_below_floor_is_excluded_even_if_nominal_total_passes(self):
        salary = parse_salary("50-80K·20薪")
        label, rejected = salary_gate(salary, CONFIG)
        self.assertTrue(rejected)
        self.assertIn("12个月固定上沿", label)

    def test_ideal_role_scores_high(self):
        job = Job(
            "Google Product Manager, Autonomous Agent Quality",
            "https://example.com/jobs/google-agent",
            "Own persistent agent roadmap, multi-step evals, tool use, browser safety and latency.",
            "test",
        )
        result = assess_job(job, CONFIG)
        self.assertGreaterEqual(result.fit, 80)
        self.assertTrue(result.eligible)

    def test_article_title_is_not_a_product_job(self):
        article = Job(
            "Jordan Mechner - Latest News",
            "https://example.com/news",
            "A page containing agent, benchmark and product manager keywords.",
            "test",
        )
        self.assertFalse(looks_like_product_job(article))

    def test_product_role_title_is_a_job_candidate(self):
        job = Job("Agent Evals 产品经理 - DeepSeek", "https://example.com/job", "", "test")
        self.assertTrue(looks_like_product_job(job))

    def test_fde_is_excluded(self):
        job = Job(
            "Forward Deployed Engineer",
            "https://example.com/jobs/fde",
            "Customer implementation, on-site delivery and frequent travel.",
            "test",
        )
        result = assess_job(job, CONFIG)
        self.assertIn("FDE", result.excluded_reason)

    def test_gtm_growth_product_role_is_excluded(self):
        job = Job(
            "GTM Growth Product Manager, Agentic Systems",
            "https://example.com/jobs/gtm",
            "Own agent launches, evals, APIs, safety, latency and business outcomes.",
            "test",
        )
        self.assertIn("GTM", assess_job(job, CONFIG).excluded_reason)

    def test_generic_ai_company_infrastructure_pm_does_not_score_as_target_role(self):
        job = Job(
            "Staff Product Manager, Infrastructure",
            "https://example.com/jobs/infra",
            "Our company builds multimodal agents. Own APIs, evals, security, reliability and latency.",
            "test",
        )
        self.assertLess(assess_job(job, CONFIG).fit, 72)

    def test_finance_agent_role_does_not_claim_driving_transfer(self):
        job = Job(
            "Senior AI Product Manager, Finance Agents",
            "https://example.com/jobs/finance",
            "The company also serves autonomous vehicle customers. Own agent evals and safety.",
            "Scale AI",
        )
        self.assertNotIn("驾驶/交通真实场景可直接迁移", assess_job(job, CONFIG).strengths)

    def test_selection_caps_global_roles_to_leave_room_for_china(self):
        global_one = assess_job(Job("Agent Product Manager", "https://example.com/1", "Agent evals", "A", scope="global"), CONFIG)
        global_two = assess_job(Job("Agent Product Manager", "https://example.com/2", "Agent evals", "B", scope="global"), CONFIG)
        china = assess_job(Job("Agent 产品经理", "https://example.com/3", "Agent 评测", "C", scope="china"), CONFIG)
        selected = select_for_push([global_one, global_two, china], max_jobs=3, max_global=1)
        self.assertEqual([item.job.source for item in selected], ["A", "C"])

    def test_salary_below_target_is_excluded(self):
        job = Job(
            "Agent 评测产品经理",
            "https://example.com/jobs/low-pay",
            "负责智能体 Benchmark 与自动评测，45-65K·16薪",
            "test",
        )
        result = assess_job(job, CONFIG)
        self.assertIn("薪酬", result.excluded_reason)

    def test_signed_webhook_is_deterministic(self):
        url = signed_webhook_url(
            "https://oapi.dingtalk.com/robot/send?access_token=token",
            "secret",
            timestamp_ms=1700000000000,
        )
        self.assertIn("timestamp=1700000000000", url)
        self.assertIn("sign=", url)
        self.assertNotIn("secret", url)

    def test_dingtalk_signature_known_vector(self):
        url = signed_webhook_url(
            "https://oapi.dingtalk.com/robot/send?access_token=dummy",
            "SECtest_secret",
            timestamp_ms=1700000000003,
        )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["sign"], "tgDPzJq1TRFIP2VyAPlxK8himNLlpKyazgARe0B/p3g=")

    def test_dingtalk_rejects_lookalike_host(self):
        with self.assertRaises(ValueError):
            validate_dingtalk_webhook(
                "https://oapi.dingtalk.com.evil.example/robot/send?access_token=dummy"
            )

    def test_parse_greenhouse_preserves_stable_id_and_official_provenance(self):
        payload = json.dumps(
            {
                "jobs": [
                    {
                        "id": 42,
                        "title": "Product Manager, Agent Evals",
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/42?gh_src=one",
                        "content": "Own <b>benchmarks</b> and reliability.",
                        "location": {"name": "San Francisco, CA"},
                    }
                ]
            }
        ).encode()
        source = {"name": "Acme 官方", "key": "greenhouse:acme", "scope": "global"}
        job = parse_greenhouse(payload, source)[0]
        self.assertTrue(job.official)
        self.assertEqual(job.location, "San Francisco, CA")
        self.assertEqual(job.source_key, "greenhouse:acme")
        same_id = Job(job.title, "https://example.com/changed", "", "renamed", "42", source_key="greenhouse:acme")
        self.assertEqual(job.identity, same_id.identity)

    def test_parse_ashby_filters_unlisted_and_reads_compensation(self):
        payload = json.dumps(
            {
                "jobs": [
                    {
                        "id": "listed",
                        "isListed": True,
                        "title": "Senior Product Manager, Agent Safety",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/listed",
                        "descriptionPlain": "Own evals and safety.",
                        "location": "New York",
                        "compensation": {"scrapeableCompensationSalarySummary": "$220K - $280K base"},
                    },
                    {
                        "id": "hidden",
                        "isListed": False,
                        "title": "Product Manager, Hidden",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/hidden",
                    },
                ]
            }
        ).encode()
        jobs = parse_ashby(payload, {"name": "Acme", "key": "ashby:acme"})
        self.assertEqual(len(jobs), 1)
        self.assertIn("$220K - $280K", jobs[0].summary)

    def test_parse_moka_builds_job_url_and_location(self):
        payload = json.dumps(
            {
                "total": 1,
                "jobs": [
                    {
                        "id": "abc",
                        "status": "open",
                        "title": "AI Agent 产品经理",
                        "description": "<p>定义 Benchmark 与 badcase 归因</p>",
                        "locations": [{"country": "中国", "province": "北京", "area": "海淀区"}],
                    }
                ],
            }
        ).encode()
        source = {
            "name": "Acme 官方",
            "key": "moka:acme",
            "scope": "china",
            "job_url_template": "https://app.mokahr.com/apply/acme/1#/job/{id}",
        }
        jobs, total = parse_moka(payload, source)
        self.assertEqual(total, 1)
        self.assertEqual(jobs[0].url, "https://app.mokahr.com/apply/acme/1#/job/abc")
        self.assertEqual(jobs[0].location, "中国·北京·海淀区")

    def test_official_job_enrichment_preserves_metadata(self):
        job = Job(
            "Agent Product Manager",
            "https://jobs.ashbyhq.com/acme/1",
            "Own evals.",
            "Acme",
            "1",
            "Shanghai",
            "china",
            True,
            "ashby:acme",
        )
        self.assertIs(enrich_jobs([job], 10)[0], job)

    def test_format_report_includes_decision_fields_and_respects_byte_limit(self):
        assessment = assess_job(
            Job(
                "Agent 评测产品经理",
                "https://app.mokahr.com/apply/acme/1#/job/abc",
                "负责 Agent Benchmark、失败归因、安全与端到端产品闭环。",
                "Acme 官方",
                "abc",
                "上海",
                "china",
                True,
                "moka:acme",
            ),
            CONFIG,
        )
        report = format_report([assessment], 1, [])
        self.assertIn("岗位重点", report)
        self.assertIn("薪酬口径", report)
        self.assertLessEqual(len(report.encode("utf-8")), 16000)

    def test_seen_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            save_seen(path, {"one", "two"})
            self.assertEqual(load_seen(path), {"one", "two"})
            self.assertIn("updated_at", json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
