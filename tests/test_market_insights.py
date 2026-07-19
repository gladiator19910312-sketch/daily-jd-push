import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar_insights import (
    analyze_market,
    append_market_snapshot,
    build_market_snapshot,
    load_market_history,
    save_market_history,
)
from radar_matching import assess_job
from radar_types import Job, SourceCoverage


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "current_fixed_cash_wan": 100.0,
    "target_total_comp_wan": 140.0,
    "usd_cny": 7.0,
    "preferred_companies": [],
    "primary_locations": ["北京", "天津", "Beijing", "Tianjin"],
    "preferred_job_age_days": 90,
    "max_job_age_days": 180,
}


def assessment(
    title: str,
    summary: str,
    *,
    company: str,
    location: str = "北京",
    source_key: str = "official:test",
    published_at: str = "2026-07-10T00:00:00Z",
    scope: str = "china",
    job_id: str = "",
):
    url_suffix = f"/{job_id}" if job_id else ""
    return assess_job(
        Job(
            title,
            f"https://example.com/jobs/{company}/{title}{url_suffix}",
            summary,
            f"{company} 官方",
            company=company,
            job_id=job_id,
            location=location,
            source_key=source_key,
            official=True,
            active=True,
            published_at=published_at,
            date_basis="published",
            scope=scope,
        ),
        CONFIG,
    )


class MarketInsightsTests(unittest.TestCase):
    def test_snapshot_uses_all_validated_jobs_and_counts_each_tag_once(self):
        values = [
            assessment(
                "高级 Agent 评测产品经理",
                "负责 Agent Agent Benchmark 评测、失败归因、trace、Python API 原型、可靠性和端到端上线。",
                company="甲公司",
            ),
            assessment(
                "多模态 Agent 产品负责人",
                "负责多模态端侧 Agent、tool calling、context、memory、成本、延迟与业务 ROI。",
                company="乙公司",
                location="天津",
            ),
        ]
        coverage = (
            SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),
            SourceCoverage("official:b", "乙公司官方", "china", "ok", 1),
            SourceCoverage("official:c", "丙公司官方", "china", "error", 0, "HTTPError"),
        )

        snapshot = build_market_snapshot(values, coverage, CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 2)
        self.assertEqual(snapshot.company_count, 2)
        self.assertEqual(snapshot.primary_count, 2)
        self.assertEqual(snapshot.directions["Evals / Benchmark / 可靠性"], 1)
        self.assertEqual(snapshot.directions["多模态与端侧"], 1)
        self.assertEqual(snapshot.skills["Python / API / 原型"], 1)
        self.assertEqual(snapshot.skills["trace / 失败归因"], 1)
        self.assertEqual(snapshot.official_sources_ok, 2)
        self.assertEqual(snapshot.official_sources_planned, 3)

    def test_snapshot_excludes_ineligible_or_non_target_product_roles(self):
        values = [
            assessment(
                "高级增长产品经理",
                "负责用户增长、市场投放、渠道营销和商业化转化，推动产品上线。",
                company="增长公司",
            ),
            assessment(
                "高级支付产品经理",
                "负责支付产品路线、端到端上线和业务结果。",
                company="支付公司",
            ),
            assessment(
                "高级 Agent 产品经理",
                "负责 Agent 产品路线、评测可靠性和端到端上线。",
                company="目标公司",
            ),
        ]

        snapshot = build_market_snapshot(values, (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 1)
        self.assertEqual(snapshot.company_count, 1)

    def test_distinct_official_requisitions_with_same_title_are_not_collapsed(self):
        values = [
            assessment(
                "Agent 产品经理",
                "负责 Agent Benchmark、评测可靠性和失败归因。",
                company="甲公司",
                job_id="req-1",
            ),
            assessment(
                "Agent 产品经理",
                "负责 Agent 平台、MCP、tool calling、context 和 memory。",
                company="甲公司",
                job_id="req-2",
            ),
        ]

        snapshot = build_market_snapshot(values, (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 2)
        self.assertEqual(snapshot.directions["Evals / Benchmark / 可靠性"], 1)
        self.assertEqual(snapshot.directions["Agent 平台 / 工具链"], 1)

    def test_distinct_moka_fragment_requisitions_are_not_collapsed(self):
        first = assessment(
            "Agent 产品经理",
            "负责 Agent Benchmark、评测可靠性和失败归因。",
            company="甲公司",
            source_key="official:moka",
            job_id="req-1",
        )
        second = assessment(
            "Agent 产品经理",
            "负责 Agent 平台、MCP、tool calling、context 和 memory。",
            company="甲公司",
            source_key="official:moka",
            job_id="req-2",
        )
        first = assess_job(
            Job(**{**first.job.__dict__, "url": "https://jobs.example.com/#/job/req-1"}),
            CONFIG,
        )
        second = assess_job(
            Job(**{**second.job.__dict__, "url": "https://jobs.example.com/#/job/req-2"}),
            CONFIG,
        )

        snapshot = build_market_snapshot([first, second], (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 2)
        self.assertEqual(snapshot.directions["Evals / Benchmark / 可靠性"], 1)
        self.assertEqual(snapshot.directions["Agent 平台 / 工具链"], 1)

    def test_same_official_url_with_and_without_job_id_is_deduped(self):
        with_id = assessment(
            "Agent 产品经理",
            "负责 Agent Benchmark、评测可靠性和端到端产品上线闭环。",
            company="甲公司",
            job_id="req-1",
        )
        without_id = assess_job(
            Job(**{**with_id.job.__dict__, "job_id": ""}),
            CONFIG,
        )

        snapshot = build_market_snapshot([with_id, without_id], (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 1)

    def test_canonical_identity_stays_on_job_id_when_longer_url_record_wins_text(self):
        with_id = assessment(
            "Agent 产品经理",
            "负责 Agent Benchmark、评测可靠性和上线闭环。",
            company="甲公司",
            job_id="req-1",
        )
        without_id = assess_job(
            Job(
                **{
                    **with_id.job.__dict__,
                    "job_id": "",
                    "source_key": "official:careers-fallback",
                    "summary": (
                        "负责 Agent Benchmark、评测可靠性、失败归因、tool calling、"
                        "成本延迟权衡和端到端产品上线闭环，正文比 API 初筛记录更完整。"
                    ),
                }
            ),
            CONFIG,
        )

        day_one = build_market_snapshot([with_id, without_id], (), CONFIG, now=NOW)
        day_two = build_market_snapshot([with_id], (), CONFIG, now=NOW + timedelta(days=1))

        self.assertEqual(day_one.sample_identities, day_two.sample_identities)
        self.assertEqual(day_one.sample_identities, (with_id.job.identity,))
        self.assertEqual(day_one.sample_identity_sources, day_two.sample_identity_sources)
        self.assertEqual(day_one.sample_identity_sources[with_id.job.identity], "official:test")

    def test_snapshot_separates_primary_china_other_and_global_denominators(self):
        values = [
            assessment(
                "Agent 产品经理",
                "负责 Agent 产品路线、任务闭环、可靠性评测和端到端业务结果。",
                company="北京公司",
                location="北京",
            ),
            assessment(
                "AI Product Lead",
                "Own AI product outcomes and multimodal evaluation.",
                company="海外公司",
                location="San Francisco, CA",
                scope="global",
            ),
        ]

        snapshot = build_market_snapshot(values, (), CONFIG, now=NOW)

        self.assertEqual(snapshot.segments["北京/天津"]["sample_count"], 1)
        self.assertEqual(snapshot.segments["海外"]["sample_count"], 1)
        self.assertEqual(
            snapshot.segments["北京/天津"]["directions"].get("多模态与端侧", 0),
            0,
        )
        self.assertEqual(snapshot.segments["海外"]["directions"]["多模态与端侧"], 1)

    def test_generic_senior_product_title_is_kept_when_jd_owns_agent_evals(self):
        generic_title = assessment(
            "Principal Product Manager",
            "Own the Agent evaluation roadmap, benchmark acceptance criteria, reliability and product launch outcomes.",
            company="甲公司",
        )

        snapshot = build_market_snapshot([generic_title], (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 1)

    def test_content_signals_are_not_part_of_job_supply_snapshot(self):
        value = assessment(
            "Agent 产品经理",
            "负责 Agent 产品路线、任务闭环和上线。",
            company="甲公司",
        )
        snapshot = build_market_snapshot(
            [value],
            (SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),),
            CONFIG,
            now=NOW,
        )

        self.assertEqual(snapshot.sample_count, 1)
        self.assertFalse(hasattr(snapshot, "content_count"))

    def test_snapshot_excludes_roles_older_than_hard_freshness_cap(self):
        old = assessment(
            "Agent 评测产品经理",
            "负责 Agent Benchmark、评测、可靠性和产品上线闭环。",
            company="甲公司",
            published_at="2025-12-01T00:00:00Z",
        )

        snapshot = build_market_snapshot(
            [old],
            (SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),),
            CONFIG,
            now=NOW,
        )

        self.assertEqual(snapshot.sample_count, 0)
        self.assertNotIn("180天以上", snapshot.freshness)

    def test_snapshot_quarantines_implausible_future_publish_date(self):
        future = assessment(
            "Agent 评测产品经理",
            "负责 Agent Benchmark、评测、可靠性和产品上线闭环。",
            company="甲公司",
            published_at="2027-01-01T00:00:00Z",
        )

        snapshot = build_market_snapshot([future], (), CONFIG, now=NOW)

        self.assertEqual(snapshot.sample_count, 0)

    def test_trend_claim_waits_for_28_distinct_days(self):
        value = assessment(
            "Agent 评测产品经理",
            "负责 Agent Benchmark、评测和可靠性。",
            company="甲公司",
        )
        coverage = (SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),)
        current = build_market_snapshot([value], coverage, CONFIG, now=NOW)
        short_history = []
        for days_ago in range(10, 0, -1):
            snapshot = build_market_snapshot(
                [value], coverage, CONFIG, now=NOW - timedelta(days=days_ago)
            )
            short_history = append_market_snapshot(short_history, snapshot)

        insight = analyze_market(current, short_history)

        self.assertEqual(insight.history_days, 11)
        self.assertEqual(insight.timing_label, "基线积累中")
        self.assertEqual(insight.direction_changes, ())
        self.assertIn("28", insight.timing_reason)

    def test_history_round_trip_replaces_same_day_snapshot(self):
        first = build_market_snapshot([], (), CONFIG, now=NOW)
        later_same_day = build_market_snapshot(
            [
                assessment(
                    "Agent 产品经理",
                    "负责 Agent 产品路线、评测和上线。",
                    company="甲公司",
                )
            ],
            (SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),),
            CONFIG,
            now=NOW + timedelta(hours=2),
        )
        history = append_market_snapshot([], first)
        history = append_market_snapshot(history, later_same_day)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market_history.json"
            save_market_history(path, history)
            loaded = load_market_history(path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["sample_count"], 1)

    def test_history_resets_when_methodology_changes(self):
        current = build_market_snapshot([], (), CONFIG, now=NOW)
        incompatible = {
            **current.__dict__,
            "snapshot_date": "2026-07-18",
            "captured_at": "2026-07-18T12:00:00+00:00",
            "methodology_id": "v1-old-method",
        }

        history = append_market_snapshot([incompatible], current)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["methodology_id"], current.methodology_id)

    def test_methodology_changes_with_salary_or_search_discovery_inputs(self):
        base = {
            **CONFIG,
            "queries": [{"name": "官方兜底", "query": "site:careers.example.com Agent"}],
            "trusted_job_hosts": ["careers.example.com"],
            "job_search_budget_seconds": 180,
            "max_results_per_query": 12,
        }
        original = build_market_snapshot([], (), base, now=NOW)
        changed_rate = build_market_snapshot([], (), {**base, "usd_cny": 8.0}, now=NOW)
        changed_queries = build_market_snapshot([], (), {**base, "queries": []}, now=NOW)

        self.assertNotEqual(original.methodology_id, changed_rate.methodology_id)
        self.assertNotEqual(original.methodology_id, changed_queries.methodology_id)

    def test_unhealthy_historical_sources_cannot_emit_warming_claim(self):
        value = assessment(
            "Agent 评测产品经理",
            "负责 Agent Benchmark、评测和可靠性。",
            company="甲公司",
        )
        healthy = (SourceCoverage("official:a", "甲公司官方", "china", "ok", 1),)
        current = build_market_snapshot([value], healthy, CONFIG, now=NOW)
        history = []
        for days_ago in range(27, 0, -1):
            row = build_market_snapshot(
                [value], healthy, CONFIG, now=NOW - timedelta(days=days_ago)
            )
            data = {**row.__dict__, "source_keys_ok": ()}
            history = append_market_snapshot(history, data)

        insight = analyze_market(current, history)

        self.assertEqual(insight.history_days, 28)
        self.assertEqual(insight.timing_label, "样本口径变化，暂缓判断")
        self.assertEqual(insight.direction_changes, ())

    def test_28_day_timing_uses_radar_first_seen_inflow_not_active_stock_age(self):
        coverage = (SourceCoverage("official:a", "甲公司官方", "china", "ok", 20),)
        values = [
            assessment(
                "Agent 评测产品经理",
                "负责 Agent Benchmark、评测可靠性、失败归因和产品上线闭环。",
                company="甲公司",
                source_key="official:a",
                job_id=f"req-{index}",
                published_at="2026-07-01T00:00:00Z",
            )
            for index in range(20)
        ]
        current = build_market_snapshot(values, coverage, CONFIG, now=NOW)
        identities = list(current.sample_identities)
        first_seen_days = {
            **{identity: -10 for identity in identities[:8]},
            **{identity: -3 for identity in identities[8:]},
        }
        history = []
        for days_ago in range(27, 0, -1):
            relative_day = -days_ago
            visible = {
                identity
                for identity, first_seen in first_seen_days.items()
                if first_seen <= relative_day
            }
            date = (NOW - timedelta(days=days_ago)).date().isoformat()
            row = {
                **current.__dict__,
                "snapshot_date": date,
                "captured_at": f"{date}T12:00:00+00:00",
                "sample_identities": tuple(sorted(visible)),
                "sample_identity_sources": {
                    identity: current.sample_identity_sources[identity] for identity in visible
                },
                "sample_identity_segments": {
                    identity: current.sample_identity_segments[identity] for identity in visible
                },
                "sample_identity_directions": {
                    identity: current.sample_identity_directions[identity] for identity in visible
                },
            }
            history = append_market_snapshot(history, row)

        insight = analyze_market(current, history)

        self.assertEqual(insight.history_days, 28)
        self.assertEqual(insight.timing_label, "北京/天津雷达新增供给升温，可小步试投")
        self.assertIn("近7日 12", insight.timing_reason)
        self.assertIn("此前7日 8", insight.timing_reason)


if __name__ == "__main__":
    unittest.main()
