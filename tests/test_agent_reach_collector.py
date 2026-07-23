import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "collect_agent_reach.py"
SPEC = importlib.util.spec_from_file_location("collect_agent_reach", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(collector)

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def EMPTY_SEARCHER(query, language):
    return []


class FakeRunner:
    def __init__(self, *, fail_xhs=False, fail_wechat=False):
        self.calls = []
        self.fail_xhs = fail_xhs
        self.fail_wechat = fail_wechat

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        self.assert_safe_invocation(args, kwargs)
        channel, action = args[1], args[2]
        if channel == "xiaohongshu" and self.fail_xhs:
            return subprocess.CompletedProcess(args, 1, "", "login required")
        if channel == "weixin" and self.fail_wechat:
            return subprocess.CompletedProcess(args, 1, "", "blocked")
        if channel == "xiaohongshu" and action == "search":
            payload = {
                "items": [
                    {
                        "note_id": "note-user-id-should-not-leak",
                        "xsec_token": "xhs-secret-token",
                        "title": "社招｜AI Agent 评测产品经理",
                        "desc": "负责 Agent 评测和产品闭环，联系 13800138000 或 recruiter@example.com",
                        "publish_time": "2026-05-12",
                        "author": {"nickname": "评测笔记", "user_id": "private-user-id"},
                    },
                    {
                        "note_id": "campus-note",
                        "xsec_token": "campus-token",
                        "title": "AI 产品经理校招实习生",
                        "desc": "面向应届生的 Agent 产品校招岗位，不是社招。",
                        "publish_time": "2天前",
                    },
                    {
                        "note_id": "second-professional-note",
                        "xsec_token": "second-temporary-token",
                        "title": "Agent 产品评测面试｜资深社招经验",
                        "desc": "资深 AI 产品岗位关注 Agent 工具调用评测、失败归因和上线可靠性。",
                        "publish_time": "2天前",
                    },
                ]
            }
        elif channel == "xiaohongshu" and action == "note":
            payload = [
                {"field": "title", "value": "社招｜AI Agent 评测产品经理"},
                {
                    "field": "content",
                    "value": (
                        "岗位将设计并落地 Agent 评测体系，推动从原型到上线，"
                        "并负责可靠性、失败归因和产品闭环。联系 13800138000，"
                        "recruiter@example.com，xsec_token=must-not-leak"
                    ),
                },
            ]
        elif channel == "weixin":
            payload = [
                {
                    "title": "高阶 AI Agent 产品经理社招观察",
                    "summary": "文章梳理了企业对 Agent 评测闭环、可靠性和业务结果的高阶产品人才要求。",
                    "publish_time": "07-13",
                    "url": "https://weixin.sogou.com/link?url=temporary&sogou_token=secret",
                }
            ]
        elif channel == "boss":
            return subprocess.CompletedProcess(args, 1, "", "login required")
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload, ensure_ascii=False), "")

    @staticmethod
    def assert_safe_invocation(args, kwargs):
        if not isinstance(args, list):
            raise AssertionError("must use argv list")
        if kwargs.get("shell"):
            raise AssertionError("must not use a shell")


class EmptyXhsDetailRunner(FakeRunner):
    def __call__(self, args, **kwargs):
        if args[1:3] == ["xiaohongshu", "note"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return super().__call__(args, **kwargs)


class PartialXhsRunner(FakeRunner):
    def __call__(self, args, **kwargs):
        if args[1:3] == ["xiaohongshu", "search"] and args[3] == collector.XHS_QUERIES[1]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            return subprocess.CompletedProcess(args, 1, "", "temporary failure")
        return super().__call__(args, **kwargs)


class MultiWechatRunner(FakeRunner):
    def __call__(self, args, **kwargs):
        if args[1:3] == ["weixin", "search"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            index = collector.WECHAT_QUERIES.index(args[3])
            payload = [
                {
                    "title": f"AI Agent 高阶产品人才观察 {index}",
                    "summary": (
                        f"第 {index} 组公开摘要分析 Agent 产品社招、评测可靠性、"
                        "人才流动和业务闭环要求。"
                    ),
                    "publish_time": "07-13",
                }
            ]
            return subprocess.CompletedProcess(
                args, 0, json.dumps(payload, ensure_ascii=False), ""
            )
        return super().__call__(args, **kwargs)


class DuplicateWechatRunner(FakeRunner):
    def __init__(self, *, fail_last=False):
        super().__init__()
        self.fail_last = fail_last

    def __call__(self, args, **kwargs):
        if args[1:3] == ["weixin", "search"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            if self.fail_last and args[3] == collector.WECHAT_QUERIES[-1]:
                return subprocess.CompletedProcess(args, 1, "", "temporary failure")
            payload = [
                {
                    "title": "高阶 AI Agent 产品经理社招观察",
                    "summary": "文章梳理企业对 Agent 评测闭环、可靠性和业务结果的高阶产品人才要求。",
                    "publish_time": "07-13",
                }
            ]
            return subprocess.CompletedProcess(
                args, 0, json.dumps(payload, ensure_ascii=False), ""
            )
        return super().__call__(args, **kwargs)


class AgentReachCollectorTests(unittest.TestCase):
    def test_collects_detail_text_and_never_serializes_tokens_or_pii(self):
        runner = FakeRunner()
        sleeps = []

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=sleeps.append, searcher=EMPTY_SEARCHER)
        serialized = json.dumps(payload, ensure_ascii=False).casefold()

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["coverage"]), 5)
        self.assertEqual({item["channel"] for item in payload["items"]}, {"xiaohongshu", "wechat"})
        xhs = next(item for item in payload["items"] if item["channel"] == "xiaohongshu")
        self.assertIn("失败归因", xhs["summary"])
        self.assertEqual(xhs["url"], "")
        self.assertIn("邮箱已脱敏", xhs["summary"])
        wechat = next(item for item in payload["items"] if item["channel"] == "wechat")
        self.assertEqual(wechat["url"], "")
        self.assertEqual(wechat["published_at"], "2026-07-12T16:00:00+00:00")
        wechat_coverage = next(
            row for row in payload["coverage"] if row["channel"] == "wechat"
        )
        self.assertEqual(wechat_coverage["queries"], 6)
        searched = {
            call[3]
            for call, _ in runner.calls
            if call[1:3] == ["weixin", "search"]
        }
        self.assertEqual(searched, set(collector.WECHAT_QUERIES))
        for forbidden in (
            "xsec_token", "xhs-secret-token", "sogou_token", "private-user-id",
            "note-user-id", "13800138000", "recruiter@example.com", "cookie", "user_id",
        ):
            self.assertNotIn(forbidden, serialized)
        note_calls = [call for call, _ in runner.calls if call[1:3] == ["xiaohongshu", "note"]]
        self.assertEqual(len(note_calls), 2)  # Duplicate search hits are read only once.
        self.assertEqual(sleeps, [collector.DETAIL_DELAY_SECONDS])

    def test_channel_failures_are_independent_and_honestly_reported(self):
        runner = FakeRunner(fail_xhs=True)

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=lambda _: None, searcher=EMPTY_SEARCHER)
        statuses = {row["channel"]: row["status"] for row in payload["coverage"]}

        self.assertEqual(statuses["xiaohongshu"], "auth_required")
        self.assertEqual(statuses["wechat"], "ok")
        self.assertEqual(statuses["maimai"], "no_results")
        self.assertEqual(statuses["boss"], "auth_required")
        self.assertTrue(any(item["channel"] == "wechat" for item in payload["items"]))

    def test_wechat_failure_does_not_discard_xhs_and_dates_normalize(self):
        runner = FakeRunner(fail_wechat=True)

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=lambda _: None, searcher=EMPTY_SEARCHER)
        statuses = {row["channel"]: row["status"] for row in payload["coverage"]}
        xhs = next(
            item
            for item in payload["items"]
            if item["channel"] == "xiaohongshu" and item["title"].startswith("社招")
        )

        self.assertEqual(statuses["xiaohongshu"], "ok")
        self.assertEqual(statuses["wechat"], "error")
        self.assertEqual(xhs["published_at"], "2026-05-11T16:00:00+00:00")

    def test_private_writer_sets_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "supplement.json"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)
            collector._write_private(path, {"schema_version": 1})

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_xhs_note_id_provides_an_approximate_publish_date(self):
        value = collector._xhs_url_date(
            "https://www.xiaohongshu.com/search_result/697f6c74000000000702a893?xsec_token=temporary"
        )

        self.assertTrue(value.startswith("2026-02-01T"))

    def test_title_and_hashtags_are_not_treated_as_detail_evidence(self):
        title = "如何衡量你做的 AI 产品好不好"
        shell = title + " #AI #产品经理 #大模型 #Agent #人工智能"
        detail = "正文解释了任务集设计、对照实验、成功率、成本、稳定性和安全性如何共同构成评测闭环。"

        self.assertFalse(collector._has_substantive_detail(title, shell))
        self.assertTrue(collector._has_substantive_detail(title, detail))

    def test_unknown_dates_are_not_treated_as_recent(self):
        self.assertFalse(collector._is_recent("", NOW))
        self.assertFalse(collector._is_recent("not-a-date", NOW))

    def test_wechat_original_link_keeps_only_stable_public_parameters(self):
        url = collector._stable_wechat_url(
            {
                "url": (
                    "http://mp.weixin.qq.com/s?mid=2&__biz=a&idx=1&sn=x"
                    "&scene=27&from=timeline#wechat_redirect"
                )
            }
        )

        self.assertEqual(
            url,
            "https://mp.weixin.qq.com/s?__biz=a&idx=1&mid=2&sn=x",
        )

    def test_empty_detail_response_does_not_increment_detail_reads(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=EmptyXhsDetailRunner(), sleeper=lambda _: None, searcher=EMPTY_SEARCHER
        )
        coverage = next(
            row for row in payload["coverage"] if row["channel"] == "xiaohongshu"
        )

        self.assertEqual(coverage["detail_reads"], 0)
        self.assertFalse(any(item["channel"] == "xiaohongshu" for item in payload["items"]))

    def test_partial_query_failure_keeps_successful_channel_results(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=PartialXhsRunner(), sleeper=lambda _: None, searcher=EMPTY_SEARCHER
        )
        coverage = next(
            row for row in payload["coverage"] if row["channel"] == "xiaohongshu"
        )

        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["queries"], 2)
        self.assertIn("部分查询失败", coverage["summary"])
        self.assertTrue(any(item["channel"] == "xiaohongshu" for item in payload["items"]))

    def test_wechat_searches_six_families_and_caps_output_at_two(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=MultiWechatRunner(), sleeper=lambda _: None, searcher=EMPTY_SEARCHER
        )
        coverage = next(row for row in payload["coverage"] if row["channel"] == "wechat")
        items = [item for item in payload["items"] if item["channel"] == "wechat"]

        self.assertEqual(coverage["queries"], 6)
        self.assertEqual(coverage["raw_count"], 6)
        self.assertEqual(coverage["relevant_count"], 6)
        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["source"] for item in items}), 2)

    def test_wechat_relevant_count_is_unique_across_query_families(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=DuplicateWechatRunner(), sleeper=lambda _: None, searcher=EMPTY_SEARCHER
        )
        coverage = next(row for row in payload["coverage"] if row["channel"] == "wechat")

        self.assertEqual(coverage["raw_count"], 6)
        self.assertEqual(coverage["relevant_count"], 1)

    def test_wechat_partial_failure_is_not_reported_as_full_success(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=DuplicateWechatRunner(fail_last=True), sleeper=lambda _: None,
            searcher=EMPTY_SEARCHER,
        )
        coverage = next(row for row in payload["coverage"] if row["channel"] == "wechat")

        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["relevant_count"], 1)
        self.assertIn("部分查询失败", coverage["summary"])

    def test_wechat_rejects_generic_newsletters_with_incidental_agent_text(self):
        self.assertFalse(
            collector._is_relevant_wechat(
                "央企名企北京地区岗位推荐 7 月",
                "列表同时包含 AI Agent 工程师实习生和高级投资经理。",
            )
        )
        self.assertTrue(
            collector._is_relevant_wechat(
                "高阶 AI Agent 产品经理社招观察",
                "文章梳理了 Agent 评测闭环、可靠性和业务结果的产品人才要求。",
            )
        )


class BossFakeRunner(FakeRunner):
    def __init__(self, *, fail_search=False, flaky_detail=False, not_logged_in=False):
        super().__init__()
        self.fail_search = fail_search
        self.flaky_detail = flaky_detail
        self.not_logged_in = not_logged_in
        self.detail_attempts = 0

    def __call__(self, args, **kwargs):
        if args[1:3] == ["boss", "search"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            if self.not_logged_in:
                return subprocess.CompletedProcess(args, 1, "", "login required")
            if self.fail_search:
                return subprocess.CompletedProcess(args, 1, "", "Detached while handling command.")
            payload = [
                {
                    "name": "Agent评测产品经理",
                    "salary": "40-65K·16薪",
                    "company": "美团",
                    "area": "北京·朝阳区·望京",
                    "experience": "3-5年",
                    "degree": "本科",
                    "skills": "评测体系,Agent",
                    "boss": "岳女士 · 招聘者",
                    "security_id": "sid-meituan-1",
                    "url": "https://www.zhipin.com/job_detail/abc123DEF.html",
                },
                {
                    "name": "Agent 产品实习生",
                    "salary": "3-4K",
                    "company": "某司",
                    "area": "上海·浦东",
                    "security_id": "sid-shanghai-2",
                    "url": "https://www.zhipin.com/job_detail/zzz999YYY.html",
                },
            ]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload, ensure_ascii=False), "")
        if args[1:3] == ["boss", "detail"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            self.detail_attempts += 1
            if self.flaky_detail and self.detail_attempts == 1:
                return subprocess.CompletedProcess(args, 1, "", "Detached while handling command.")
            payload = [
                {
                    "name": "Agent评测产品经理",
                    "city": "北京",
                    "district": "朝阳区",
                    "description": (
                        "岗位职责：设计 Agent 能力评测体系，拆解意图理解、多轮规划、工具调用，"
                        "建立端到端产品评测框架；主导高复杂度评测集设计，构建自动化数据筛选与合成策略。"
                        "任职要求：3 年以上 AI 产品经验，熟悉大模型评测与失败归因。"
                        "联系 13800138000 或 recruiter@example.com。"
                    ),
                }
            ]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload, ensure_ascii=False), "")
        return super().__call__(args, **kwargs)


class BossCollectorTests(unittest.TestCase):
    def test_boss_jobs_collected_sanitized_and_filtered(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=BossFakeRunner(), sleeper=lambda _: None, searcher=EMPTY_SEARCHER
        )
        boss_jobs = [job for job in payload["jobs"] if job["channel"] == "boss"]
        self.assertEqual(len(boss_jobs), 1)
        job = boss_jobs[0]
        self.assertEqual(job["title"], "Agent评测产品经理")
        self.assertIn("北京", job["location"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("岳女士", serialized)
        self.assertNotIn("13800138000", job["description"])
        self.assertNotIn("recruiter@example.com", job["description"])
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["boss"]["status"], "ok")
        self.assertEqual(coverage["boss"]["detail_reads"], 1)

    def test_boss_detail_retry_recovers_from_detach(self):
        runner = BossFakeRunner(flaky_detail=True)
        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=lambda _: None, searcher=EMPTY_SEARCHER)
        self.assertGreaterEqual(runner.detail_attempts, 2)
        self.assertEqual(len(payload["jobs"]), 1)

    def test_boss_auth_required_marks_coverage(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=BossFakeRunner(not_logged_in=True), sleeper=lambda _: None,
            searcher=EMPTY_SEARCHER,
        )
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["boss"]["status"], "auth_required")
        self.assertEqual(payload["jobs"], [])

    def test_boss_search_failure_marks_error_not_zero(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=BossFakeRunner(fail_search=True), sleeper=lambda _: None,
            searcher=EMPTY_SEARCHER,
        )
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["boss"]["status"], "error")
        self.assertEqual(payload["jobs"], [])


LIEPIN_MD = """# 【北京 Agent 产品专家（评测方向）招聘】-智谱招聘信息
> 原文链接: https://www.liepin.com/job/1977000001.shtml

【 北京-海淀区 】

30-55k·14薪

职位描述：
负责 Agent 评测体系设计与落地，覆盖工具调用、多轮规划与失败归因场景，
建设自动化评测集与线上回归机制，推动模型质量与产品体验闭环。
任职要求：5 年以上 AI 产品经验，熟悉大模型评测方法，有 Agent 产品实践，
能独立定义评测指标并推动跨团队落地。长期招聘，社招岗位。
""" + "补充正文。" * 60


class LiepinFakeRunner(FakeRunner):
    def __init__(self, *, short_body=False):
        super().__init__()
        self.short_body = short_body

    def __call__(self, args, **kwargs):
        if args[1:3] == ["web", "read"]:
            self.calls.append((args, kwargs))
            self.assert_safe_invocation(args, kwargs)
            cwd = kwargs.get("cwd")
            assert cwd, "web read must run inside a temp cwd"
            saved = Path(cwd) / "web-articles" / "job" / "job.md"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(
                LIEPIN_MD if not self.short_body else "# 短正文\n太少",
                encoding="utf-8",
            )
            payload = [{"title": "job", "status": "success", "saved": "web-articles/job/job.md"}]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload, ensure_ascii=False), "")
        return super().__call__(args, **kwargs)


def _liepin_searcher(query, language):
    return [
        collector.TrendSignal(
            "【北京 Agent 产品专家（评测方向）招聘】",
            "https://www.liepin.com/job/1977000001.shtml?d_sfrom=recom",
            "智谱招聘 Agent 产品专家，负责评测体系，社招岗位。",
            "猎聘公开索引·北京直招",
            "platform",
            NOW.isoformat(),
        )
    ]


def _maimai_searcher(query, language):
    return [
        collector.TrendSignal(
            "AI Agent 高阶产品人才流动观察",
            "https://maimai.cn/article/detail?efid=abc",
            "脉脉职言讨论大模型 Agent 产品负责人薪酬与流动趋势，涉及多家大厂社招。" * 2,
            "脉脉社招高阶线索",
            "platform",
            NOW.isoformat(),
        )
    ]


class LiepinMaimaiCollectorTests(unittest.TestCase):
    def test_liepin_verified_page_becomes_job(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=LiepinFakeRunner(), sleeper=lambda _: None,
            searcher=_liepin_searcher,
        )
        liepin_jobs = [job for job in payload["jobs"] if job["channel"] == "liepin"]
        self.assertEqual(len(liepin_jobs), 1)
        job = liepin_jobs[0]
        self.assertIn("30-55k·14薪", job["salary_text"])
        self.assertEqual(job["url"], "https://www.liepin.com/job/1977000001.shtml")
        self.assertGreaterEqual(len(job["description"]), 300)
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["liepin"]["status"], "ok")
        self.assertEqual(coverage["liepin"]["detail_reads"], 1)

    def test_liepin_thin_page_produces_no_job(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=LiepinFakeRunner(short_body=True), sleeper=lambda _: None,
            searcher=_liepin_searcher,
        )
        self.assertEqual([j for j in payload["jobs"] if j["channel"] == "liepin"], [])
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["liepin"]["detail_reads"], 0)

    def test_maimai_signal_without_url(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=LiepinFakeRunner(), sleeper=lambda _: None,
            searcher=_maimai_searcher,
        )
        maimai = [item for item in payload["items"] if item["channel"] == "maimai"]
        self.assertEqual(len(maimai), 1)
        self.assertEqual(maimai[0]["url"], "")
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertIn(coverage["maimai"]["status"], {"ok", "no_results"})
        self.assertNotEqual(coverage["maimai"]["status"], "unsupported")


class ReviewFixTests(unittest.TestCase):
    def test_liepin_all_queries_failed_marks_error_not_partial(self):
        def failing_searcher(query, language):
            raise OSError("network down")

        payload = collector.collect_agent_reach(
            now=NOW, runner=LiepinFakeRunner(), sleeper=lambda _: None,
            searcher=failing_searcher,
        )
        coverage = {row["channel"]: row for row in payload["coverage"]}
        self.assertEqual(coverage["liepin"]["status"], "error")
        self.assertEqual(coverage["maimai"]["status"], "error")

    def test_maimai_cap_holds_across_multiple_queries(self):
        config = {
            "trend_queries": [
                {"name": "脉脉查询一", "query": "q1", "language": "zh-CN"},
                {"name": "脉脉查询二", "query": "q2", "language": "zh-CN"},
            ]
        }

        def rich_searcher(query, language):
            return [
                collector.TrendSignal(
                    f"AI Agent 产品人才观察 {index}",
                    f"https://maimai.cn/article/detail?efid=x{index}",
                    "脉脉职言讨论大模型 Agent 产品负责人薪酬与流动趋势，涉及多家大厂社招。" * 2,
                    "脉脉社招高阶线索",
                    "platform",
                    NOW.isoformat(),
                )
                for index in range(4)
            ]

        coverage, items = collector._collect_maimai(NOW, config, rich_searcher)
        self.assertLessEqual(len(items), collector.MAIMAI_MAX_ITEMS)
        # 达到上限后提前退出：第二个查询未执行，queries 只计实际执行数。
        self.assertEqual(coverage["raw_count"], 4)
        self.assertEqual(coverage["queries"], 1)

    def test_web_read_rejects_path_traversal(self):
        class TraversalRunner(FakeRunner):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["web", "read"]:
                    payload = [{"status": "success", "saved": "../escape.md"}]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                return super().__call__(args, **kwargs)

        payload = collector.collect_agent_reach(
            now=NOW, runner=TraversalRunner(), sleeper=lambda _: None,
            searcher=_liepin_searcher,
        )
        self.assertEqual([j for j in payload["jobs"] if j["channel"] == "liepin"], [])

    def test_boss_location_falls_back_to_search_city_not_constant(self):
        class TianjinOnlyRunner(FakeRunner):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["boss", "search"]:
                    if args[5] != "天津":
                        return subprocess.CompletedProcess(args, 0, "[]", "")
                    payload = [
                        {
                            "name": "Agent 产品负责人",
                            "salary": "40-60K",
                            "company": "天津某科技公司",
                            "security_id": "sid-tianjin-only",
                            "url": "https://www.zhipin.com/job_detail/tj123abc.html",
                        }
                    ]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                if args[1:3] == ["boss", "detail"]:
                    payload = [
                        {
                            "name": "Agent 产品负责人",
                            "description": (
                                "负责 Agent 评测体系设计与落地，覆盖工具调用与失败归因，"
                                "建设自动化评测集与线上回归机制，推动产品闭环。" * 2
                            ),
                        }
                    ]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                return super().__call__(args, **kwargs)

        payload = collector.collect_agent_reach(
            now=NOW, runner=TianjinOnlyRunner(), sleeper=lambda _: None,
            searcher=EMPTY_SEARCHER,
        )
        boss_jobs = [job for job in payload["jobs"] if job["channel"] == "boss"]
        self.assertEqual(len(boss_jobs), 1)
        self.assertIn("天津", boss_jobs[0]["location"])

    def test_boss_search_url_with_query_params_is_normalized(self):
        class QueryUrlRunner(BossFakeRunner):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["boss", "search"]:
                    payload = [
                        {
                            "name": "Agent评测产品经理",
                            "salary": "40-65K·16薪",
                            "company": "美团",
                            "area": "北京·朝阳区·望京",
                            "security_id": "sid-query-url",
                            "url": "https://www.zhipin.com/job_detail/abc123DEF.html?ka=track&lid=x",
                        }
                    ]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                return super().__call__(args, **kwargs)

        payload = collector.collect_agent_reach(
            now=NOW, runner=QueryUrlRunner(), sleeper=lambda _: None,
            searcher=EMPTY_SEARCHER,
        )
        boss_jobs = [job for job in payload["jobs"] if job["channel"] == "boss"]
        self.assertEqual(len(boss_jobs), 1)
        self.assertEqual(boss_jobs[0]["url"], "https://www.zhipin.com/job_detail/abc123DEF.html")

    def test_liepin_page_without_location_is_dropped(self):
        class NoLocationRunner(LiepinFakeRunner):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["web", "read"]:
                    cwd = kwargs.get("cwd")
                    saved = Path(cwd) / "web-articles" / "job" / "job.md"
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    saved.write_text(
                        LIEPIN_MD.replace("【 北京-海淀区 】", ""), encoding="utf-8"
                    )
                    payload = [{"status": "success", "saved": "web-articles/job/job.md"}]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                return super().__call__(args, **kwargs)

        payload = collector.collect_agent_reach(
            now=NOW, runner=NoLocationRunner(), sleeper=lambda _: None,
            searcher=_liepin_searcher,
        )
        self.assertEqual([j for j in payload["jobs"] if j["channel"] == "liepin"], [])

    def test_liepin_recruiter_name_card_stays_out_of_description(self):
        class RecruiterNameRunner(LiepinFakeRunner):
            def __call__(self, args, **kwargs):
                if args[1:3] == ["web", "read"]:
                    cwd = kwargs.get("cwd")
                    saved = Path(cwd) / "web-articles" / "job" / "job.md"
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    saved.write_text(
                        "# 【北京 Agent 产品专家招聘】-某公司\n\n【 北京-朝阳区 】\n\n40-60k·14薪\n\n"
                        "猎头顾问 王晓芳 活跃\n\n职位描述：\n负责 Agent 评测体系设计与落地，"
                        "覆盖工具调用与失败归因，建设自动化评测集与线上回归机制。"
                        + "补充正文。" * 60
                        + "\n相似职位\n更多推荐",
                        encoding="utf-8",
                    )
                    payload = [{"status": "success", "saved": "web-articles/job/job.md"}]
                    return subprocess.CompletedProcess(
                        args, 0, json.dumps(payload, ensure_ascii=False), ""
                    )
                return super().__call__(args, **kwargs)

        payload = collector.collect_agent_reach(
            now=NOW, runner=RecruiterNameRunner(), sleeper=lambda _: None,
            searcher=_liepin_searcher,
        )
        liepin_jobs = [job for job in payload["jobs"] if job["channel"] == "liepin"]
        self.assertEqual(len(liepin_jobs), 1)
        self.assertNotIn("王晓芳", liepin_jobs[0]["description"])


if __name__ == "__main__":
    unittest.main()
