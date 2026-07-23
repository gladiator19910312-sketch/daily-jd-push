import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from radar_delivery import (
    format_action_report,
    format_market_report,
    send_dingtalk,
    truncate_utf8,
)
from radar_supplement import SupplementCoverage
from radar_types import Assessment, Job, Salary, TrendSignal


CONFIG = {
    "timezone": "Asia/Shanghai",
    "preferred_job_age_days": 90,
    "max_job_age_days": 180,
}


def sample_assessment() -> Assessment:
    return Assessment(
        job=Job(
            "Senior Agent Evals Product Manager",
            "https://careers.example.com/jobs/agent-evals",
            "Own Agent evaluations and product outcomes.",
            "Example 官方",
            location="北京",
            official=True,
            company="Example",
            active=True,
            published_at="2026-07-10T00:00:00Z",
            date_basis="published",
        ),
        fit=88,
        ready=72,
        asset="可迁移的 Agent Evals 产品与实验闭环",
        salary=Salary(label="未披露"),
        salary_gate="需确认现金及股票可兑现口径",
        responsibilities=("定义 Benchmark、失败归因与产品验收闭环",),
        strengths=("安全敏感高频真实场景与复杂任务定义",),
        gaps=("Python / API 端到端原型与 trace 实践",),
        work_risk="待面试确认双休、21 点后工作频率和差旅",
    )


class DeliveryFormattingTests(unittest.TestCase):
    def test_truncation_never_cuts_inside_a_markdown_link(self):
        value = "开头\n" + "[完整链接](https://example.com/" + "a" * 400 + ")\n结尾"

        result = truncate_utf8(value, 120)

        self.assertNotIn("https://example.com/", result)
        self.assertIn("完整行边界截断", result)
        self.assertLessEqual(len(result.encode("utf-8")), 120)

    def test_action_report_is_concise_and_has_own_line_cta(self):
        report = format_action_report([sample_assessment()], 31, [], config=CONFIG)

        self.assertIn("核心任务", report)
        self.assertIn("权限判断", report)
        self.assertIn("关键缺口", report)
        self.assertIn("薪酬", report)
        self.assertIn("强度 / 差旅", report)
        self.assertIn(
            "\n[查看企业官网 JD →](https://careers.example.com/jobs/agent-evals)\n",
            report,
        )
        card = report.split("### 1.", 1)[1].split("\n\n", 1)[0]
        self.assertLessEqual(len(card.splitlines()), 12)
        self.assertLessEqual(len(report.encode("utf-8")), 16_000)

    def test_market_report_exposes_denominators_and_evidence_boundaries(self):
        snapshot = {
            "sample_count": 4,
            "company_count": 3,
            "primary_count": 3,
            "directions": {"Evals / Benchmark / 可靠性": 3, "Agent 应用": 2},
            "skills": {"Python / API / 原型": 2},
            "locations": {"北京/天津": 3, "其他": 1},
            "freshness": {"90 天内": 4},
            "salary_disclosed_count": 1,
            "official_sources_ok": 2,
            "official_sources_planned": 3,
            "segments": {
                "北京/天津": {
                    "sample_count": 3,
                    "directions": {"Evals / Benchmark / 可靠性": 3, "Agent 应用": 2},
                    "skills": {"Python / API / 原型": 2},
                    "freshness": {"90 天内": 3},
                    "salary_disclosed_count": 1,
                    "work_boundary_signal_count": 0,
                    "published_date_known_count": 3,
                    "new_postings_7d": 1,
                    "previous_postings_7d": 1,
                    "new_postings_28d": 3,
                },
                "中国其他城市": {
                    "sample_count": 1,
                    "directions": {"Agent 应用": 1},
                },
            },
        }
        insight = {
            "history_days": 7,
            "timing_label": "基线积累中",
            "timing_reason": "历史不足 28 个独立日，不做趋势宣称。",
            "direction_changes": (),
            "actions": ("继续积累官网样本，并优先验证北京高阶岗位。",),
        }
        official = (
            SimpleNamespace(label="甲公司官网", status="ok"),
            SimpleNamespace(label="乙公司官网", status="no_results"),
            SimpleNamespace(label="丙公司官网", status="error"),
        )
        trend_coverage = (
            SimpleNamespace(name="猎聘公开索引·北京", status="no_results", raw_count=12, accepted_count=0),
            SimpleNamespace(name="猎聘公开索引·天津", status="ok", raw_count=8, accepted_count=1),
        )

        report = format_market_report(
            snapshot,
            insight,
            official_coverage=official,
            trend_coverage=trend_coverage,
            config=CONFIG,
        )

        self.assertIn("本轮仅使用 L1 企业官网完整正文，n=4", report)
        self.assertIn("北京 / 天津求职决策样本｜n=3", report)
        self.assertIn("Evals / Benchmark / 可靠性 3/3 (100%)", report)
        self.assertIn("薪酬披露：** 1/3 (33%)", report)
        self.assertIn("中国其他城市：** n=1｜横向机会参考", report)
        self.assertIn("企业官方源：** 正常 2/3", report)
        self.assertIn("猎聘：** 实际执行 2/2 组（成功 2 / 失败 0）", report)
        self.assertIn("原始 20 条", report)
        self.assertIn("逐查询合格 L3 1 条（可跨查询重复）", report)
        self.assertIn("L3 搜索索引线索", report)

    def test_market_report_lists_actual_execution_by_platform_family(self):
        trend_coverage = (
            SimpleNamespace(name="猎聘公开索引·北京", kind="platform", status="ok", raw_count=9, accepted_count=1),
            SimpleNamespace(name="51job 岗位线索", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="智联岗位线索", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="LinkedIn 行业岗位", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="Indeed 中国岗位", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="国聘岗位线索", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="就业在线岗位线索", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
            SimpleNamespace(name="北方人才网社招线索", kind="platform", status="skipped_disabled", raw_count=0, accepted_count=0),
        )

        report = format_market_report(
            {"sample_count": 0},
            trend_coverage=trend_coverage,
            config=CONFIG,
        )

        self.assertIn("配置不等于已执行", report)
        self.assertIn("猎聘：** 实际执行 1/1 组（成功 1 / 失败 0）｜原始 9 条", report)
        for family in (
            "51job / 前程无忧",
            "智联招聘",
            "LinkedIn",
            "Indeed",
            "国聘",
            "就业在线 / 公共就业 / 人才网",
        ):
            self.assertIn(
                f"{family}：** 本轮未执行（原因：公开检索关闭 / 无可靠正文验活）",
                report,
            )
        self.assertIn("`/job/<数字>.shtml`", report)
        self.assertIn("`/a/<数字>.shtml`", report)

    def test_market_report_lists_public_content_execution_by_family(self):
        coverage = (
            SimpleNamespace(name="微信公众号·高阶社招", kind="content", status="ok", raw_count=12, accepted_count=2),
            SimpleNamespace(name="微信公众号·评测可靠性", kind="content", status="error", raw_count=0, accepted_count=0),
            SimpleNamespace(name="人社部人工智能招聘专项", kind="content", status="no_results", raw_count=5, accepted_count=0),
            SimpleNamespace(name="36氪 AI 人才报告", kind="content", status="skipped_budget", raw_count=0, accepted_count=0),
        )

        report = format_market_report(
            {"sample_count": 0},
            trend_coverage=coverage,
            config=CONFIG,
        )

        self.assertIn("公众号公开索引：** 实际执行 2/2 组（成功 1 / 失败 1）", report)
        self.assertIn("原始 12 条｜逐查询合格 L4 2 条（可跨查询重复）", report)
        self.assertIn("政府 / 公共就业内容：** 实际执行 1/1 组", report)
        self.assertIn("行业媒体 / 报告：** 实际执行 0/1 组", report)

    def test_market_report_distinguishes_budget_skips_from_disabled_sources(self):
        report = format_market_report(
            {"sample_count": 0},
            trend_coverage=(
                SimpleNamespace(
                    name="猎聘公开索引·北京",
                    kind="platform",
                    status="skipped_budget",
                    raw_count=0,
                    accepted_count=0,
                ),
            ),
            config=CONFIG,
        )

        self.assertIn("本轮未执行（原因：达到本轮时间预算）", report)
        self.assertNotIn("实际执行 1/1", report)

    def test_market_report_does_not_call_all_error_queries_no_results(self):
        report = format_market_report(
            {"sample_count": 0},
            trend_coverage=(
                SimpleNamespace(
                    name="猎聘公开索引·北京",
                    kind="platform",
                    status="error",
                    raw_count=0,
                    accepted_count=0,
                ),
            ),
            config=CONFIG,
        )

        self.assertIn("实际执行 1/1 组（成功 0 / 失败 1）", report)
        self.assertIn("查询已发起但全部失败", report)
        self.assertIn("不能据此推断“无合格岗位”", report)
        self.assertNotIn("成功完成的查询中无合格", report)

    def test_market_report_has_clickable_lead_and_search_fallback(self):
        observed = "2026-07-19T04:00:00+00:00"
        liepin = TrendSignal(
            "AI Agent 高级产品经理",
            "https://www.liepin.com/a/77939203.shtml",
            "北京社招，负责多模态 Agent 评测与产品闭环。",
            "猎聘公开索引",
            "platform",
            observed,
        )
        xhs = TrendSignal(
            "Agent 产品人才趋势",
            "https://www.xiaohongshu.com/explore/abc?xsec_token=SECRET",
            "讨论 Agent 评测、可靠性与高阶产品人才需求。",
            "小红书（Agent Reach 实读）",
            "content",
            observed,
        )
        coverage = (SupplementCoverage("xiaohongshu", "ok", "已实读", 3, 20, 4, 2),)

        report = format_market_report(
            {"sample_count": 0},
            signals=[liepin, xhs],
            source_coverage=coverage,
            config=CONFIG,
        )

        self.assertIn(
            "[打开岗位线索并确认仍在招 →](https://www.liepin.com/a/77939203.shtml)",
            report,
        )
        self.assertIn("原文无稳定直达链接", report)
        self.assertIn(
            "[在小红书按标题检索 →](https://www.xiaohongshu.com/search_result?keyword=Agent+%E4%BA%A7%E5%93%81%E4%BA%BA%E6%89%8D%E8%B6%8B%E5%8A%BF)",
            report,
        )
        self.assertNotIn("SECRET", report)
        self.assertLessEqual(len(report.encode("utf-8")), 16_000)

    def test_l4_corroboration_uses_all_current_evidence_not_only_new_display_items(self):
        evidence = TrendSignal(
            "Agent 评测与可靠性人才报告",
            "https://example.com/agent-evals-report",
            "高阶社招关注 Benchmark、trace 与安全评测。",
            "行业报告",
            "content",
            "2026-07-19T04:00:00+00:00",
            published_at="",
        )

        report = format_market_report(
            {"sample_count": 0},
            signals=[],
            evidence_signals=[evidence],
            config=CONFIG,
        )

        self.assertIn("在 45 天内 0/1", report)
        self.assertIn("交叉佐证仍使用本轮检索到的有效证据存量", report)

    @patch("radar_delivery.urllib.request.build_opener")
    def test_send_dingtalk_uses_custom_payload_title(self, mock_build_opener):
        response = Mock()
        response.read.return_value = b'{"errcode": 0, "errmsg": "ok"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        mock_build_opener.return_value = opener

        send_dingtalk(
            "## 内容",
            "https://oapi.dingtalk.com/robot/send?access_token=test-token",
            "test-secret",
            title="Sunny AI 求职市场情报",
        )

        request = opener.open.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["markdown"]["title"], "Sunny AI 求职市场情报")
        self.assertEqual(payload["markdown"]["text"], "## 内容")


def platform_assessment() -> Assessment:
    return Assessment(
        job=Job(
            "Agent评测产品经理",
            "https://www.zhipin.com/job_detail/abc123DEF.html",
            "负责 Agent 评测体系设计与落地，覆盖工具调用与失败归因。",
            "BOSS直聘（本机验活）",
            location="北京·朝阳区",
            official=False,
            company="美团",
            active=True,
        ),
        fit=80,
        ready=60,
        asset="Agent Benchmark 与上线闭环案例",
        salary=Salary(label="40-65K·16薪"),
        salary_gate="公开薪酬未触发红线",
        responsibilities=("设计 Agent 评测体系",),
        strengths=("Benchmark/评测主轴匹配",),
        gaps=("Agent 原型/API 实验闭环证据需补齐",),
        work_risk="工时/差旅未披露",
    )


class PlatformJobSectionTests(unittest.TestCase):
    def test_platform_section_rendered_with_items(self):
        report = format_action_report(
            [], 0, [], platform_items=[platform_assessment()], config=CONFIG
        )

        self.assertIn("平台岗位｜BOSS / 猎聘（本机已验活 L2）", report)
        self.assertIn("平台信息以企业官网与面试确认为准", report)
        self.assertIn("https://www.zhipin.com/job_detail/abc123DEF.html", report)
        self.assertIn("发布日期未披露", report)

    def test_platform_section_omitted_without_items(self):
        report = format_action_report([], 0, [], config=CONFIG)

        self.assertNotIn("平台岗位｜BOSS / 猎聘", report)


class DingtalkRetryTests(unittest.TestCase):
    WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=test-token"

    @staticmethod
    def _ok_response():
        response = Mock()
        response.read.return_value = b'{"errcode": 0, "errmsg": "ok"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    @patch("radar_delivery.time.sleep")
    @patch("radar_delivery.urllib.request.build_opener")
    def test_send_recovers_from_transient_network_error(self, mock_build_opener, _sleep):
        import urllib.error

        opener = Mock()
        opener.open.side_effect = [
            urllib.error.URLError("timed out"),
            self._ok_response(),
        ]
        mock_build_opener.return_value = opener

        send_dingtalk("## 内容", self.WEBHOOK, "test-secret")

        self.assertEqual(opener.open.call_count, 2)

    @patch("radar_delivery.time.sleep")
    @patch("radar_delivery.urllib.request.build_opener")
    def test_send_raises_after_exhausting_retries(self, mock_build_opener, _sleep):
        import urllib.error

        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("timed out")
        mock_build_opener.return_value = opener

        with self.assertRaises(RuntimeError):
            send_dingtalk("## 内容", self.WEBHOOK, "test-secret")

        self.assertEqual(opener.open.call_count, 3)

    @patch("radar_delivery.time.sleep")
    @patch("radar_delivery.urllib.request.build_opener")
    def test_send_does_not_retry_client_errors(self, mock_build_opener, _sleep):
        import urllib.error

        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            self.WEBHOOK, 400, "Bad Request", {}, None
        )
        mock_build_opener.return_value = opener

        with self.assertRaises(RuntimeError):
            send_dingtalk("## 内容", self.WEBHOOK, "test-secret")

        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
