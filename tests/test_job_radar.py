import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from job_radar import (
    Job,
    assess_job,
    enrich_jobs,
    format_report,
    load_seen,
    load_seen_state,
    looks_like_product_job,
    parse_ashby,
    parse_greenhouse,
    parse_moka,
    parse_rss,
    parse_salary,
    parse_tencent,
    parse_duckduckgo_lite,
    parse_trend_rss,
    partition_market,
    salary_gate,
    save_seen,
    select_for_push,
    select_diverse_assessments,
    select_signals,
    signals_to_baseline,
    signed_webhook_url,
    validate_dingtalk_webhook,
    was_seen,
)
from radar_market import job_freshness, location_bucket, parse_timestamp
from radar_trends import discover_trend_signals, signal_is_recent
from radar_types import TrendSignal, normalize_url


CONFIG = {
    "current_fixed_cash_wan": 100.0,
    "target_total_comp_wan": 140.0,
    "usd_cny": 7.0,
    "preferred_companies": ["Google", "DeepSeek", "京东"],
    "primary_locations": ["北京", "天津", "Beijing", "Tianjin"],
    "preferred_job_age_days": 90,
    "max_job_age_days": 180,
    "max_primary_push_jobs": 4,
    "max_trend_push_jobs": 3,
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

    def test_parse_trend_rss_preserves_index_date_and_kind(self):
        payload = b"""<?xml version='1.0'?><rss><channel><item>
        <title>Agent product hiring report</title><link>https://example.com/report/1</link>
        <description>Agent evals hiring trend</description>
        <pubDate>Sat, 18 Jul 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""
        signals = parse_trend_rss(payload, "industry", "content", 10)
        self.assertEqual(signals[0].kind, "content")
        self.assertEqual(signals[0].indexed_at, "Sat, 18 Jul 2026 08:00:00 GMT")

    def test_parse_duckduckgo_lite_decodes_redirect_and_observation_time(self):
        payload = b"""<html><body><table>
        <tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.zhaopin.com%2Fjobdetail%2F42.htm&amp;rut=x" class="result-link">AI Agent Product Manager</a></td></tr>
        <tr><td class="result-snippet">Beijing role owning agent evals and hiring.</td></tr>
        </table></body></html>"""
        observed = datetime(2026, 7, 19, 4, 0, tzinfo=timezone.utc)
        signal = parse_duckduckgo_lite(payload, "智联", "platform", 1, observed)[0]
        self.assertEqual(signal.url, "https://www.zhaopin.com/jobdetail/42.htm")
        self.assertEqual(signal.summary, "Beijing role owning agent evals and hiring.")
        self.assertEqual(signal.indexed_at, "2026-07-19T04:00:00+00:00")

    def test_signal_selection_reserves_room_for_content(self):
        platform = [
            parse_trend_rss(
                f"<rss><channel><item><title>Agent product hiring {index}</title><link>https://example.com/p{index}</link><description>AI product jobs</description><pubDate>Sat, 18 Jul 2026 08:00:00 GMT</pubDate></item></channel></rss>".encode(),
                f"platform-{index}",
                "platform",
                1,
            )[0]
            for index in range(3)
        ]
        content = parse_trend_rss(
            b"<rss><channel><item><title>Agent hiring report</title><link>https://example.com/report</link><description>AI product talent trend</description><pubDate>Sat, 18 Jul 2026 08:00:00 GMT</pubDate></item></channel></rss>",
            "report",
            "content",
            1,
        )[0]
        selected = select_signals([*platform, content], 3)
        self.assertEqual([signal.kind for signal in selected], ["platform", "platform", "content"])

    def test_signal_selection_rotates_to_unseen_content_source(self):
        observed = "2026-07-19T04:00:00+00:00"
        wechat = TrendSignal(
            "Agent hiring report",
            "https://mp.weixin.qq.com/s?__biz=a&mid=1&idx=1&sn=one",
            "AI product talent trend",
            "WeChat",
            "content",
            observed,
        )
        xiaohongshu = TrendSignal(
            "AI product careers",
            "https://www.xiaohongshu.com/explore/one?xsec_token=abc",
            "Agent product hiring trend",
            "Xiaohongshu",
            "content",
            observed,
        )
        selected = select_signals([wechat, xiaohongshu], 1, {"WeChat"})
        self.assertEqual(selected, [xiaohongshu])
        self.assertEqual(signals_to_baseline([wechat, xiaohongshu], selected), [xiaohongshu])

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

    def test_primary_selection_prefers_distinct_employers(self):
        first = assess_job(Job("Agent Product Manager", "https://e/1", "Agent evals", "A"), CONFIG)
        duplicate = assess_job(Job("Agent Product Manager", "https://e/2", "Agent evals", "A"), CONFIG)
        second = assess_job(Job("Agent Product Manager", "https://e/3", "Agent evals", "B"), CONFIG)
        selected = select_diverse_assessments([first, duplicate, second], 2)
        self.assertEqual([item.job.source for item in selected], ["A", "B"])

    def test_location_bucket_prioritizes_beijing_and_tianjin(self):
        self.assertEqual(location_bucket(Job("PM", "https://e/1", "", "A", location="北京 / 上海"), CONFIG), "primary")
        self.assertEqual(location_bucket(Job("PM", "https://e/2", "", "A", location="Tianjin"), CONFIG), "primary")
        self.assertEqual(location_bucket(Job("PM", "https://e/3", "", "A", location="深圳", scope="china"), CONFIG), "china_other")
        self.assertEqual(location_bucket(Job("PM", "https://e/4", "", "A", location="San Francisco", scope="global"), CONFIG), "global")
        self.assertEqual(location_bucket(Job("PM", "https://e/5", "", "A", location="全国远程", scope="china"), CONFIG), "unknown")

    def test_freshness_uses_published_date_and_hard_cap(self):
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        fresh = Job("PM", "https://e/1", "", "A", official=True, published_at="2026-04-20T00:00:00Z", date_basis="published", active=True)
        old = Job("PM", "https://e/2", "", "A", official=True, published_at="2026-01-19T00:00:00Z", date_basis="published", active=True)
        self.assertTrue(job_freshness(fresh, CONFIG, now).primary_eligible)
        self.assertFalse(job_freshness(old, CONFIG, now).trend_eligible)

    def test_recent_official_update_is_actionable_but_not_called_published(self):
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        job = Job("PM", "https://e/1", "", "A", official=True, published_at="2026-07-18T00:00:00Z", date_basis="updated", active=True)
        freshness = job_freshness(job, CONFIG, now)
        self.assertTrue(freshness.primary_eligible)
        self.assertTrue(freshness.trend_eligible)
        self.assertIn("非发布时间", freshness.label)

    def test_partition_market_keeps_overseas_out_of_primary_pool(self):
        now = datetime(2026, 7, 19, tzinfo=timezone.utc)
        primary = assess_job(Job("Agent 产品经理", "https://e/1", "Agent 评测", "A", location="北京", scope="china", official=True, published_at="2026-07-01", date_basis="published", active=True), CONFIG)
        global_job = assess_job(Job("Agent Product Manager", "https://e/2", "Agent evals", "B", location="New York", scope="global", official=True, published_at="2026-07-01", date_basis="published", active=True), CONFIG)
        main, trends = partition_market([global_job, primary], CONFIG, now)
        self.assertEqual([item.job.source for item in main], ["A"])
        self.assertEqual([item.job.source for item in trends], ["B"])

    def test_trend_seen_state_can_upgrade_to_primary_once(self):
        identity = "job-1"
        seen = {f"trend:{identity}"}
        self.assertFalse(was_seen(seen, "primary", identity))
        self.assertTrue(was_seen(seen, "trend", identity))
        self.assertTrue(was_seen({f"primary:{identity}"}, "trend", identity))

    def test_signal_recency_uses_index_time_only_as_content_filter(self):
        payload = b"""<?xml version='1.0'?><rss><channel><item>
        <title>AI Agent recruitment report</title><link>https://example.com/r</link>
        <description>Product and evals trend</description>
        <pubDate>Sat, 18 Jul 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""
        signal = parse_trend_rss(payload, "industry", "content", 1)[0]
        self.assertTrue(signal_is_recent(signal, 45, datetime(2026, 7, 19, tzinfo=timezone.utc)))

    @patch("radar_trends.http_get")
    def test_trend_discovery_falls_back_when_primary_search_fails(self, mock_get):
        fallback = b"""<rss><channel><item>
        <title>Agent product hiring report</title><link>https://example.com/report</link>
        <description>AI product talent and recruitment trend</description>
        <pubDate>Sat, 18 Jul 2026 08:00:00 GMT</pubDate>
        </item></channel></rss>"""
        mock_get.side_effect = [urllib.error.URLError("blocked"), fallback]
        signals, failures = discover_trend_signals(
            {
                "max_results_per_query": 3,
                "trend_signal_max_age_days": 45,
                "trend_signal_hosts": ["example.com"],
                "trend_queries": [
                    {"name": "report", "kind": "content", "query": "Agent hiring report"}
                ],
            }
        )
        self.assertEqual([signal.url for signal in signals], ["https://example.com/report"])
        self.assertEqual(failures, [])
        self.assertEqual(parse_timestamp(signals[0].indexed_at).date(), datetime.now(timezone.utc).date())

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
                        "updated_at": "2026-07-01T00:00:00Z",
                    }
                ]
            }
        ).encode()
        source = {"name": "Acme 官方", "key": "greenhouse:acme", "scope": "global"}
        job = parse_greenhouse(payload, source)[0]
        self.assertTrue(job.official)
        self.assertEqual(job.location, "San Francisco, CA")
        self.assertEqual(job.source_key, "greenhouse:acme")
        self.assertEqual(job.date_basis, "updated")
        self.assertTrue(job.active)
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
                        "publishedAt": "2026-07-02T00:00:00Z",
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
        self.assertEqual(jobs[0].date_basis, "published")

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
                        "publishedAt": "2026-07-03T00:00:00Z",
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
        self.assertEqual(jobs[0].date_basis, "published")

    def test_parse_tencent_keeps_official_update_and_beijing_location(self):
        payload = json.dumps(
            {
                "Code": 200,
                "Data": {
                    "Count": 1,
                    "Posts": [
                        {
                            "PostId": "42",
                            "RecruitPostName": "元宝-大模型评测产品经理",
                            "LocationName": "北京",
                            "Responsibility": "建设自动评测系统、Benchmark 与用户数据闭环",
                            "Requirement": "熟悉 Agent 产品",
                            "LastUpdateTime": "2026年07月17日",
                            "PostURL": "jobdesc.html?postId=42",
                        }
                    ],
                },
            }
        ).encode()
        jobs, total = parse_tencent(payload, {"name": "腾讯官方招聘", "key": "tencent:official"})
        self.assertEqual(total, 1)
        self.assertEqual(jobs[0].location, "北京")
        self.assertEqual(jobs[0].date_basis, "updated")
        self.assertEqual(jobs[0].published_at, "2026-07-17T00:00:00+00:00")
        self.assertTrue(jobs[0].url.startswith("https://careers.tencent.com/"))

    def test_eval_product_title_beats_adjacent_data_strategy_role(self):
        eval_role = assess_job(
            Job(
                "元宝-大模型评测产品经理",
                "https://e/1",
                "建设自动评测系统、Benchmark 与用户数据闭环。",
                "腾讯官方招聘",
            ),
            CONFIG,
        )
        data_role = assess_job(
            Job(
                "多模态数据策略产品经理",
                "https://e/2",
                "负责多模态 Benchmark、数据生产与安全评测。",
                "腾讯官方招聘",
            ),
            CONFIG,
        )
        self.assertGreater(eval_role.fit, data_role.fit)
        self.assertTrue(any("数据策略" in gap for gap in data_role.gaps))

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

    @patch("radar_discovery.http_get")
    def test_enrichment_rejects_soft_404_without_jobposting_schema(self, mock_get):
        mock_get.return_value = (
            "<html><body>普通招聘首页 2026-07-19 " + "导航与公司介绍 " * 30 + "</body></html>"
        ).encode()
        job = Job(
            "Agent Product Manager",
            "https://careers.example.com/jobs/42",
            "Search snippet with Agent evals.",
            "search",
        )
        result = enrich_jobs([job], 1, ["careers.example.com"])[0]
        self.assertIs(result, job)
        self.assertFalse(result.official)

    @patch("radar_discovery.http_get")
    def test_enrichment_uses_authoritative_description_not_search_snippet(self, mock_get):
        mock_get.return_value = b"""<html><body>Agent product role details and application information repeated enough for validation. Agent product role details and application information repeated enough for validation.
        <script type="application/ld+json">{"@type":"JobPosting","title":"Agent Product Manager","description":"Own Agent evals, reliability, and product outcomes.","datePosted":"2026-07-01"}</script>
        </body></html>"""
        job = Job(
            "Agent Product Manager",
            "https://careers.example.com/jobs/42",
            "Untrusted search snippet says 1-2K monthly.",
            "search",
        )
        result = enrich_jobs([job], 1, ["careers.example.com"])[0]
        self.assertTrue(result.official)
        self.assertEqual(result.summary, "Own Agent evals, reliability, and product outcomes.")

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
        assessment = assess_job(
            Job(
                **{**assessment.job.__dict__, "published_at": "2026-07-01", "date_basis": "published", "active": True}
            ),
            CONFIG,
        )
        report = format_report([assessment], 1, [], config=CONFIG)
        self.assertIn("岗位重点", report)
        self.assertIn("薪酬口径", report)
        self.assertIn("北京 / 天津", report)
        self.assertLessEqual(len(report.encode("utf-8")), 16000)

    def test_report_escapes_untrusted_markdown(self):
        signal = TrendSignal(
            "报告](https://evil.example)[详情",
            "https://mp.weixin.qq.com/s?__biz=a&mid=1&idx=1&sn=safe",
            "Agent 招聘 [恶意链接](https://evil.example)",
            "公众号",
            "content",
            "2026-07-19T04:00:00+00:00",
        )
        report = format_report([], 0, [], signals=[signal], config=CONFIG)
        self.assertIn("报告\\]", report)
        self.assertEqual(report.count("](https://"), 1)

    def test_url_normalization_is_host_aware_and_stable(self):
        xhs_one = "https://www.xiaohongshu.com/explore/abc?xsec_token=one&xsec_source=pc"
        xhs_two = "https://www.xiaohongshu.com/explore/abc?xsec_token=two&xsec_source=search"
        self.assertEqual(normalize_url(xhs_one), normalize_url(xhs_two))
        wechat_one = "https://mp.weixin.qq.com/s?mid=2&__biz=a&idx=1&sn=x&from=timeline"
        wechat_two = "https://mp.weixin.qq.com/s?sn=x&idx=1&__biz=a&mid=2"
        self.assertEqual(normalize_url(wechat_one), normalize_url(wechat_two))
        self.assertNotEqual(
            normalize_url("https://jobs.example.com/view?ref=one"),
            normalize_url("https://jobs.example.com/view?ref=two"),
        )

    def test_seen_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            save_seen(path, {"one", "two"})
            self.assertEqual(load_seen(path), {"one", "two"})
            self.assertIn("updated_at", json.loads(path.read_text()))

    def test_seen_state_expires_items_not_observed_within_hard_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "jobs": {
                            "old": "2026-01-01T00:00:00+00:00",
                            "recent": "2026-07-01T00:00:00+00:00",
                        },
                    }
                )
            )
            state = load_seen_state(
                path,
                180,
                datetime(2026, 7, 19, tzinfo=timezone.utc),
            )
            self.assertEqual(set(state), {"recent"})


if __name__ == "__main__":
    unittest.main()
