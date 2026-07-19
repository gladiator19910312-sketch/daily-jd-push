import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from radar_supplement import (
    SupplementValidationError,
    load_supplement,
    parse_supplement,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def valid_payload() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-07-19T11:00:00+00:00",
        "coverage": [
            {
                "channel": "linkedin",
                "status": "ok",
                "summary": "Completed two senior-role searches and read one public job page.",
                "queries": 2,
                "raw_count": 8,
                "relevant_count": 1,
                "detail_reads": 1,
            },
            {
                "channel": "xiaohongshu",
                "status": "no_results",
                "summary": "Completed two searches; no recent senior evidence survived review.",
                "queries": 2,
                "raw_count": 14,
                "relevant_count": 0,
                "detail_reads": 2,
            },
        ],
        "items": [
            {
                "channel": "linkedin",
                "kind": "platform",
                "evidence": "detail_read",
                "title": "Senior Product Manager, Agent Evaluation",
                "summary": "The public JD assigns ownership of agent evaluation, reliability metrics, and launch decisions.",
                "url": "https://www.linkedin.com/jobs/view/12345/?trk=public_jobs",
                "published_at": "2026-07-12T00:00:00Z",
                "observed_at": "2026-07-19T10:55:00Z",
            }
        ],
    }


class RadarSupplementTests(unittest.TestCase):
    def test_loads_sanitized_signals_and_preserves_coverage(self):
        bundle = parse_supplement(valid_payload(), now=NOW)

        self.assertEqual(bundle.schema_version, 1)
        self.assertEqual(len(bundle.signals), 1)
        self.assertEqual(bundle.signals[0].kind, "platform")
        self.assertEqual(bundle.signals[0].source, "LinkedIn")
        self.assertEqual(bundle.signals[0].url, "https://www.linkedin.com/jobs/view/12345")
        self.assertEqual(bundle.signals[0].indexed_at, "2026-07-19T10:55:00+00:00")
        self.assertEqual(bundle.signals[0].published_at, "2026-07-12T00:00:00+00:00")
        self.assertEqual(bundle.coverage[0].queries, 2)
        self.assertEqual(bundle.coverage[1].status, "no_results")

    def test_partial_coverage_may_keep_items_from_successful_queries(self):
        payload = valid_payload()
        payload["coverage"][0]["status"] = "partial"

        bundle = parse_supplement(payload, now=NOW)

        self.assertEqual(bundle.coverage[0].status, "partial")
        self.assertEqual(len(bundle.signals), 1)

    def test_loads_file_and_allows_honest_non_clickable_evidence(self):
        payload = valid_payload()
        payload["coverage"] = [
            {
                "channel": "xiaohongshu",
                "status": "ok",
                "summary": "One relevant note was read, but its temporary permalink was removed.",
                "queries": 1,
                "raw_count": 3,
                "relevant_count": 1,
                "detail_reads": 1,
            }
        ]
        payload["items"] = [
            {
                "channel": "xiaohongshu",
                "kind": "content",
                "evidence": "detail_read",
                "source": "小红书（Agent Reach 实读）｜评测观察",
                "title": "Agent 产品高阶社招观察",
                "summary": "正文讨论了高阶 Agent 产品岗位对评测闭环、上线可靠性和失败归因的要求，但没有稳定公开链接。",
                "published_at": "2026-07-13T00:00:00Z",
                "observed_at": "2026-07-19T10:56:00Z",
                "url": "",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "supplement.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            bundle = load_supplement(path, now=NOW)

        self.assertEqual(bundle.signals[0].url, "")
        self.assertEqual(bundle.signals[0].kind, "content")
        self.assertTrue(bundle.signals[0].source.endswith("评测观察"))

    def test_rejects_stale_or_future_payload(self):
        stale = valid_payload()
        stale["generated_at"] = "2026-07-17T23:59:59+00:00"
        future = valid_payload()
        future["generated_at"] = "2026-07-19T12:06:00+00:00"

        with self.assertRaises(SupplementValidationError):
            parse_supplement(stale, now=NOW)
        with self.assertRaises(SupplementValidationError):
            parse_supplement(future, now=NOW)

    def test_rejects_sensitive_keys_recursively_and_unknown_fields(self):
        sensitive_keys = (
            "cookie", "Authorization", "raw_response", "rawHtml",
            "xsecToken", "sogou token", "userId", "accessToken", "passTicket",
        )
        for key in sensitive_keys:
            with self.subTest(key=key):
                payload = valid_payload()
                payload["metadata"] = {"nested": [{key: "secret"}]}
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

        payload = valid_payload()
        payload["items"][0]["company"] = "Example"
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

    def test_rejects_sensitive_values_and_contact_details(self):
        values = (
            "正文含 xsec_token=temporary-value，不应导出到 Actions。",
            "联系 recruiter@example.com 获取完整岗位说明。",
            "可拨打 13800138000 进一步沟通岗位。",
        )
        for value in values:
            with self.subTest(value=value):
                payload = valid_payload()
                payload["items"][0]["summary"] = value + " 此处补足有效长度。"
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

    def test_rejects_sensitive_url_parameters_userinfo_and_fragments(self):
        urls = (
            "https://www.linkedin.com/jobs/view/12345/?xsec_token=secret",
            "https://www.linkedin.com/jobs/view/12345/?access_token=secret",
            "https://www.linkedin.com/jobs/view/12345/?pass_ticket=secret",
            "https://user:pass@www.linkedin.com/jobs/view/12345/",
            "https://www.linkedin.com/jobs/view/12345/#token",
        )
        for url in urls:
            with self.subTest(url=url):
                payload = valid_payload()
                payload["items"][0]["url"] = url
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

    def test_rejects_private_unknown_channel_mismatch_and_unstable_xhs_url(self):
        urls = (
            "http://127.0.0.1/jobs/view/12345",
            "https://example.com/jobs/view/12345",
            "https://www.xiaohongshu.com/explore/123",
            "https://linkedin.com.evil.example/jobs/view/12345",
        )
        for url in urls:
            with self.subTest(url=url):
                payload = valid_payload()
                payload["items"][0]["url"] = url
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

        payload = valid_payload()
        payload["coverage"] = [{
            "channel": "xiaohongshu", "status": "ok", "queries": 1,
            "raw_count": 1, "relevant_count": 1, "detail_reads": 1,
        }]
        payload["items"][0].update({
            "channel": "xiaohongshu", "kind": "content",
            "url": "https://www.xiaohongshu.com/explore/123",
        })
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

    def test_linkedin_requires_direct_detail_and_boss_items_remain_closed(self):
        payload = valid_payload()
        payload["items"][0]["url"] = "https://www.linkedin.com/jobs/search/"
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)
        payload["items"][0]["url"] = (
            "https://www.linkedin.com/jobs/view/senior-product-manager-12345/"
        )
        self.assertTrue(parse_supplement(payload, now=NOW).signals[0].url)

        payload["coverage"] = [{
            "channel": "boss", "status": "ok", "queries": 1,
            "raw_count": 1, "relevant_count": 1, "detail_reads": 1,
        }]
        payload["items"][0].update({
            "channel": "boss",
            "url": "https://www.zhipin.com/job_detail/abc123.html",
        })
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

    def test_rejects_template_or_thin_summaries(self):
        summaries = (
            "高级 Agent 产品经理",
            "BOSS直聘为您提供2026年北京评测产品经理信息，在线开聊约面试，找工作就上BOSS直聘。",
            "点击查看详情，登录后查看更多内容；此处只是为凑足长度而重复的无效平台模板文案。",
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                payload = valid_payload()
                payload["items"][0]["summary"] = summary
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

    def test_rejects_invalid_coverage_status_counts_and_missing_coverage(self):
        invalid_status = valid_payload()
        invalid_status["coverage"][0]["status"] = "maybe"
        with self.assertRaises(SupplementValidationError):
            parse_supplement(invalid_status, now=NOW)

        missing_coverage = valid_payload()
        missing_coverage["coverage"] = [missing_coverage["coverage"][1]]
        with self.assertRaises(SupplementValidationError):
            parse_supplement(missing_coverage, now=NOW)

        inconsistent = valid_payload()
        inconsistent["coverage"][0]["raw_count"] = 0
        with self.assertRaises(SupplementValidationError):
            parse_supplement(inconsistent, now=NOW)

        impossible_status = valid_payload()
        impossible_status["coverage"][0]["status"] = "no_results"
        with self.assertRaises(SupplementValidationError):
            parse_supplement(impossible_status, now=NOW)

        float_count = valid_payload()
        float_count["coverage"][0]["queries"] = 1.5
        with self.assertRaises(SupplementValidationError):
            parse_supplement(float_count, now=NOW)

    def test_rejects_invalid_or_future_item_times(self):
        for field, value in (
            ("observed_at", "not-a-date"),
            ("observed_at", "2026-07-19T11:06:00Z"),
            ("published_at", "not-a-date"),
            ("published_at", "2026-07-22T00:00:00Z"),
        ):
            with self.subTest(field=field, value=value):
                payload = valid_payload()
                payload["items"][0][field] = value
                with self.assertRaises(SupplementValidationError):
                    parse_supplement(payload, now=NOW)

    def test_rejects_duplicate_items_and_non_detail_platform_evidence(self):
        payload = valid_payload()
        payload["coverage"][0]["relevant_count"] = 2
        payload["coverage"][0]["detail_reads"] = 2
        payload["items"].append(deepcopy(payload["items"][0]))
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

        payload = valid_payload()
        payload["items"][0]["evidence"] = "search_summary"
        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

    def test_detail_read_evidence_requires_matching_coverage_count(self):
        payload = valid_payload()
        payload["coverage"] = [{
            "channel": "xiaohongshu", "status": "ok", "queries": 1,
            "raw_count": 1, "relevant_count": 1, "detail_reads": 0,
        }]
        payload["items"] = [{
            "channel": "xiaohongshu",
            "kind": "content",
            "evidence": "detail_read",
            "title": "Agent 评测产品经理岗位观察",
            "summary": "正文解释了 Agent 评测任务集、失败归因、可靠性、安全与产品闭环的具体要求。",
            "published_at": "2026-07-12T00:00:00Z",
            "observed_at": "2026-07-19T10:55:00Z",
            "url": "",
        }]

        with self.assertRaises(SupplementValidationError):
            parse_supplement(payload, now=NOW)

    def test_import_surface_exposes_only_trend_signals(self):
        bundle = parse_supplement(valid_payload(), now=NOW)
        signal = bundle.signals[0]

        self.assertFalse(hasattr(signal, "official"))
        self.assertFalse(hasattr(bundle, "jobs"))


if __name__ == "__main__":
    unittest.main()
