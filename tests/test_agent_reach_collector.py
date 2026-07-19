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


class AgentReachCollectorTests(unittest.TestCase):
    def test_collects_detail_text_and_never_serializes_tokens_or_pii(self):
        runner = FakeRunner()
        sleeps = []

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=sleeps.append)
        serialized = json.dumps(payload, ensure_ascii=False).casefold()

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["coverage"]), 4)
        self.assertEqual({item["channel"] for item in payload["items"]}, {"xiaohongshu", "wechat"})
        xhs = next(item for item in payload["items"] if item["channel"] == "xiaohongshu")
        self.assertIn("失败归因", xhs["summary"])
        self.assertEqual(xhs["url"], "")
        self.assertIn("邮箱已脱敏", xhs["summary"])
        wechat = next(item for item in payload["items"] if item["channel"] == "wechat")
        self.assertEqual(wechat["url"], "")
        self.assertEqual(wechat["published_at"], "2026-07-12T16:00:00+00:00")
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

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=lambda _: None)
        statuses = {row["channel"]: row["status"] for row in payload["coverage"]}

        self.assertEqual(statuses["xiaohongshu"], "auth_required")
        self.assertEqual(statuses["wechat"], "ok")
        self.assertEqual(statuses["maimai"], "unsupported")
        self.assertEqual(statuses["boss"], "auth_required")
        self.assertTrue(any(item["channel"] == "wechat" for item in payload["items"]))

    def test_wechat_failure_does_not_discard_xhs_and_dates_normalize(self):
        runner = FakeRunner(fail_wechat=True)

        payload = collector.collect_agent_reach(now=NOW, runner=runner, sleeper=lambda _: None)
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

    def test_empty_detail_response_does_not_increment_detail_reads(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=EmptyXhsDetailRunner(), sleeper=lambda _: None
        )
        coverage = next(
            row for row in payload["coverage"] if row["channel"] == "xiaohongshu"
        )

        self.assertEqual(coverage["detail_reads"], 0)
        self.assertFalse(any(item["channel"] == "xiaohongshu" for item in payload["items"]))

    def test_partial_query_failure_keeps_successful_channel_results(self):
        payload = collector.collect_agent_reach(
            now=NOW, runner=PartialXhsRunner(), sleeper=lambda _: None
        )
        coverage = next(
            row for row in payload["coverage"] if row["channel"] == "xiaohongshu"
        )

        self.assertEqual(coverage["status"], "ok")
        self.assertEqual(coverage["queries"], 2)
        self.assertIn("部分查询失败", coverage["summary"])
        self.assertTrue(any(item["channel"] == "xiaohongshu" for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
