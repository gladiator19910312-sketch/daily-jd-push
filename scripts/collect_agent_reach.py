#!/usr/bin/env python3
"""Collect a small, sanitized social-evidence supplement with OpenCLI.

The collector deliberately keeps browser/session material on the local machine.
Its JSON output contains only reviewed text and coverage counters; temporary
Xiaohongshu and Sogou URLs are used for reads but are never serialized.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


XHS_QUERIES = (
    "AI Agent 产品经理 社招 北京 评测",
    "Agent 评测 产品负责人 招聘",
)
WECHAT_QUERY_FAMILIES = (
    ("高阶社招", "AI Agent 高级产品经理 产品负责人 社招 北京 天津"),
    ("评测可靠性", "Agent 评测 Benchmark 可靠性 产品经理 职业"),
    ("人才流动", "大模型 Agent 高阶人才 流动 任命 招聘"),
    ("薪酬市场", "2026 AI 大模型 Agent 人才 薪酬 招聘 报告"),
    ("大厂招聘", "阿里 字节 腾讯 美团 百度 Agent 产品 社招"),
    ("创业公司", "智谱 MiniMax 阶跃星辰 月之暗面 Agent 产品 招聘"),
)
WECHAT_QUERIES = tuple(query for _, query in WECHAT_QUERY_FAMILIES)
AI_TERMS = (
    "agent", "agentic", "智能体", "ai", "llm", "大模型", "多模态",
    "评测", "eval", "benchmark", "可靠性",
)
CAREER_TERMS = (
    "产品", "招聘", "社招", "岗位", "职位", "职业", "人才", "薪资",
    "面试", "市场", "行业", "趋势", "hiring", "career", "product",
)
TARGET_PRODUCT_TERMS = (
    "ai产品", "ai 产品", "agent产品", "agent 产品", "智能体产品",
    "大模型产品", "评测产品", "产品经理", "产品负责人", "产品专家",
    "product manager", "product lead", "product owner",
)
EARLY_CAREER_ONLY = ("校招", "应届", "实习生", "暑期实习", "秋招", "春招")
SENIOR_OR_SOCIAL = ("社招", "高级", "资深", "专家", "负责人", "senior", "staff", "lead")
MAX_AGE_DAYS = 183
MAX_DETAIL_READS = 4
DETAIL_DELAY_SECONDS = 2.5

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:xsec_token|sogou_token|access_token|cookie|user_id)\b\s*[:=]\s*[^\s&]+"
)
SENSITIVE_WORD_RE = re.compile(
    r"(?i)\b(?:xsec_token|sogou_token|access_token|cookie|cookies|user_id|raw_response)\b"
)
SOCIAL_ID_RE = re.compile(
    r"(?i)(?:用户\s*ID|小红书号|微信号|vx|wx)\s*[:：=]\s*[a-z0-9_-]+"
)
SPACE_RE = re.compile(r"\s+")
HASHTAG_RE = re.compile(r"#[^#\s]+")
CHINA_TZ = timezone(timedelta(hours=8))


class ChannelError(RuntimeError):
    """An intentionally detail-free channel failure."""

    def __init__(self, message: str, status: str = "error") -> None:
        super().__init__(message)
        self.status = status


def _invoke_json(
    args: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> Any:
    """Run OpenCLI without a shell and return its decoded JSON payload."""
    try:
        completed = runner(
            args,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChannelError("OpenCLI invocation failed") from exc
    if isinstance(completed, (dict, list)):
        return completed
    if isinstance(completed, str):
        output = completed
        return_code = 0
    else:
        return_code = int(getattr(completed, "returncode", 0))
        output = str(getattr(completed, "stdout", "") or "")
    if return_code != 0:
        diagnostic = f"{output}\n{getattr(completed, 'stderr', '')}".casefold()
        auth_markers = ("login", "logged in", "auth", "200404", "登录", "未授权")
        status = "auth_required" if any(marker in diagnostic for marker in auth_markers) else "error"
        raise ChannelError("OpenCLI returned a failure", status)
    try:
        return json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ChannelError("OpenCLI returned invalid JSON") from exc


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "notes", "articles", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return _records(data)
    return []


def _value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _clean_text(value: Any, limit: int = 600) -> str:
    text = SPACE_RE.sub(" ", str(value or "")).strip()
    text = EMAIL_RE.sub("[邮箱已脱敏]", text)
    text = PHONE_RE.sub("[手机号已脱敏]", text)
    text = SECRET_ASSIGNMENT_RE.sub("[敏感字段已脱敏]", text)
    text = SOCIAL_ID_RE.sub("[社交账号已脱敏]", text)
    text = SENSITIVE_WORD_RE.sub("[敏感字段已脱敏]", text)
    return text[:limit]


def _parse_date(value: Any, now: datetime) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    local_now = now.astimezone(CHINA_TZ)
    relative = re.fullmatch(r"(\d+)\s*(分钟|小时|天)前", text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {
            "分钟": timedelta(minutes=amount),
            "小时": timedelta(hours=amount),
            "天": timedelta(days=amount),
        }[unit]
        return (local_now - delta).astimezone(timezone.utc).isoformat()
    if text.startswith("今天"):
        return local_now.isoformat()
    if text.startswith("昨天"):
        return (local_now - timedelta(days=1)).isoformat()
    normalized = text.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=CHINA_TZ)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    short = re.fullmatch(r"(\d{1,2})-(\d{1,2})", normalized)
    if short:
        parsed = datetime(local_now.year, int(short.group(1)), int(short.group(2)), tzinfo=CHINA_TZ)
        if parsed > local_now + timedelta(days=2):
            parsed = parsed.replace(year=parsed.year - 1)
        return parsed.astimezone(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA_TZ)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def _is_recent(date_text: str, now: datetime) -> bool:
    if not date_text:
        return False
    try:
        published = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = now.astimezone(timezone.utc) - published.astimezone(timezone.utc)
    return -timedelta(days=2) <= age <= timedelta(days=MAX_AGE_DAYS)


def _is_relevant(title: str, summary: str) -> bool:
    text = f"{title}\n{summary}".casefold()
    if any(term.casefold() in text for term in EARLY_CAREER_ONLY):
        senior_markers = tuple(term for term in SENIOR_OR_SOCIAL if term != "社招")
        explicit_social = "社招" in text and "不是社招" not in text and "非社招" not in text
        if not explicit_social and not any(term.casefold() in text for term in senior_markers):
            return False
    return any(term.casefold() in text for term in AI_TERMS) and any(
        term.casefold() in text for term in CAREER_TERMS
    )


def _is_relevant_wechat(title: str, summary: str) -> bool:
    """Prefer titled, target-role evidence over generic job newsletters."""
    title_folded = title.casefold()
    text = f"{title}\n{summary}".casefold()
    return (
        _is_relevant(title, summary)
        and any(term.casefold() in title_folded for term in AI_TERMS)
        and any(term.casefold() in text for term in TARGET_PRODUCT_TERMS)
    )


def _has_substantive_detail(title: str, summary: str) -> bool:
    """Reject title-plus-hashtag shells that do not contain readable evidence."""
    body = HASHTAG_RE.sub(" ", summary)
    body = body.replace(title, " ")
    body = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", body)
    return len(body) >= 30


def _detail_url(row: dict[str, Any]) -> str:
    direct = str(_value(row, "url", "link", "note_url", "noteUrl") or "").strip()
    if direct.startswith("http"):
        host = (urllib.parse.urlsplit(direct).hostname or "").casefold()
        if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
            return direct
    note_id = str(_value(row, "note_id", "noteId", "id") or "").strip()
    token = str(_value(row, "xsec_token", "xsecToken") or "").strip()
    if not note_id or not token:
        return ""
    query = urllib.parse.urlencode({"xsec_token": token, "xsec_source": "pc_search"})
    return f"https://www.xiaohongshu.com/explore/{urllib.parse.quote(note_id)}?{query}"


def _xhs_url_date(url: str) -> str:
    """Infer the approximate publication instant from a Mongo-style note ID."""
    match = re.search(r"/(?:search_result|explore|note)/([0-9a-f]{24})(?:[/?#]|$)", url, re.I)
    if not match:
        return ""
    try:
        timestamp = int(match.group(1)[:8], 16)
        if not 1_000_000_000 <= timestamp <= 4_000_000_000:
            return ""
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _detail_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("content", "desc", "description", "text", "body"):
            if payload.get(key):
                return _clean_text(payload[key])
        data = payload.get("data")
        if data is not None:
            found = _detail_text(data)
            if found:
                return found
        fields = payload.get("fields")
        if isinstance(fields, list):
            payload = fields
    if isinstance(payload, list):
        pairs: dict[str, Any] = {}
        for entry in payload:
            if isinstance(entry, dict) and "field" in entry and "value" in entry:
                pairs[str(entry["field"]).casefold()] = entry["value"]
        for key in ("content", "desc", "description", "text", "body"):
            if pairs.get(key):
                return _clean_text(pairs[key])
        for entry in payload:
            found = _detail_text(entry)
            if found:
                return found
    return ""


def _display_author(row: dict[str, Any]) -> str:
    author = _value(row, "author", "nickname", "user_name", "userName")
    if isinstance(author, dict):
        author = _value(author, "nickname", "name", "display_name")
    return _clean_text(author, 30)


def _stable_wechat_url(row: dict[str, Any]) -> str:
    value = str(_value(row, "url", "link", "article_url") or "").strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname == "mp.weixin.qq.com" and parsed.path.startswith("/s"):
        stable_keys = {"__biz", "mid", "idx", "sn"}
        query = [
            (key, item)
            for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key in stable_keys
        ]
        query.sort(key=lambda item: (item[0], item[1]))
        return urllib.parse.urlunsplit(
            ("https", "mp.weixin.qq.com", parsed.path, urllib.parse.urlencode(query), "")
        )
    return ""


def _content_priority(item: dict[str, Any], now: datetime) -> tuple[float, str]:
    text = f"{item.get('title', '')}\n{item.get('summary', '')}".casefold()
    published_text = str(item.get("published_at") or "")
    try:
        published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        age_days = max(0.0, (now - published.astimezone(timezone.utc)).total_seconds() / 86400)
    except ValueError:
        age_days = float(MAX_AGE_DAYS)
    score = max(0.0, MAX_AGE_DAYS - age_days) / 10
    if any(term in text for term in ("北京", "天津", "beijing", "tianjin")):
        score += 12
    if any(term in text for term in ("社招", "招聘", "岗位", "hiring")):
        score += 8
    if any(term in text for term in ("评测", "benchmark", "可靠", "失败归因", "roi", "岗位重构")):
        score += 7
    if any(term in text for term in ("agent", "智能体")):
        score += 4
    if any(term in text for term in ("招聘工具", "筛简历", "hr最该")):
        score -= 20
    return score, published_text


def _coverage(
    channel: str,
    status: str,
    summary: str,
    *,
    queries: int = 0,
    raw_count: int = 0,
    relevant_count: int = 0,
    detail_reads: int = 0,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "status": status,
        "summary": summary,
        "queries": queries,
        "raw_count": raw_count,
        "relevant_count": relevant_count,
        "detail_reads": detail_reads,
    }


def _collect_xhs(
    now: datetime,
    runner: Callable[..., Any],
    sleeper: Callable[[float], None],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    errors: list[ChannelError] = []
    successful_queries = 0
    for query in XHS_QUERIES:
        try:
            payload = _invoke_json(
                ["opencli", "xiaohongshu", "search", query, "--limit", "10", "-f", "json"],
                runner,
            )
        except ChannelError as exc:
            errors.append(exc)
            continue
        successful_queries += 1
        rows.extend(_records(payload))
    if not successful_queries and errors:
        raise errors[0]
    candidates: list[tuple[dict[str, Any], str, str, str]] = []
    seen_candidates: set[str] = set()
    for row in rows:
        title = _clean_text(_value(row, "title", "name"), 160)
        preview = _clean_text(_value(row, "summary", "desc", "description", "content"))
        published = _parse_date(_value(row, "publish_time", "published_at", "time", "date"), now)
        if not published:
            published = _xhs_url_date(_detail_url(row))
        if title and _is_relevant(title, preview) and _is_recent(published, now):
            identity = str(_value(row, "note_id", "noteId", "id") or title).casefold()
            if identity in seen_candidates:
                continue
            seen_candidates.add(identity)
            candidates.append((row, title, preview, published))
    items: list[dict[str, Any]] = []
    detail_reads = 0
    detail_attempts = 0
    for row, title, preview, published in candidates[:MAX_DETAIL_READS]:
        url = _detail_url(row)
        if not url:
            continue
        if detail_attempts:
            sleeper(DETAIL_DELAY_SECONDS)
        detail_attempts += 1
        try:
            detail = _invoke_json(["opencli", "xiaohongshu", "note", url, "-f", "json"], runner)
        except ChannelError:
            continue
        summary = _detail_text(detail)
        if (
            len(summary) < 30
            or not _is_relevant(title, summary)
            or not _has_substantive_detail(title, summary)
        ):
            continue
        detail_reads += 1
        author = _display_author(row)
        source = "小红书（Agent Reach 实读）"
        if author:
            source = f"{source}｜{author}"
        items.append(
            {
                "channel": "xiaohongshu",
                "kind": "content",
                "evidence": "detail_read",
                "source": source,
                "title": title,
                "summary": summary,
                "published_at": published,
                "observed_at": now.isoformat(timespec="seconds"),
                "url": "",
            }
        )
    items.sort(key=lambda item: _content_priority(item, now), reverse=True)
    status = "partial" if errors else "ok" if candidates else "no_results"
    summary = (
        (
            "已完成本机登录态检索与候选正文实读；部分查询失败，已保留其余结果；"
            "临时访问参数未进入补充包。"
            if errors
            else "已完成本机登录态检索与候选正文实读；临时访问参数未进入补充包。"
        )
        if items
        else "已完成本机检索，本轮没有通过时效性、相关性和正文检查的内容。"
    )
    return _coverage(
        "xiaohongshu", status, summary, queries=len(XHS_QUERIES), raw_count=len(rows),
        relevant_count=len(candidates), detail_reads=detail_reads,
    ), items


def _collect_wechat(
    now: datetime,
    runner: Callable[..., Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    errors: list[ChannelError] = []
    successful_queries = 0
    for family, query in WECHAT_QUERY_FAMILIES:
        try:
            payload = _invoke_json(
                ["opencli", "weixin", "search", query, "--limit", "10", "-f", "json"],
                runner,
            )
        except ChannelError as exc:
            errors.append(exc)
            continue
        successful_queries += 1
        rows.extend((family, row) for row in _records(payload))
    if not successful_queries and errors:
        raise errors[0]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for family, row in rows:
        title = _clean_text(_value(row, "title", "name"), 160)
        summary = _clean_text(_value(row, "summary", "desc", "description", "content"))
        published = _parse_date(_value(row, "publish_time", "published_at", "time", "date"), now)
        if (
            not title
            or len(summary) < 30
            or not _is_relevant_wechat(title, summary)
            or not _is_recent(published, now)
        ):
            continue
        identity = f"{title.casefold()}\n{summary.casefold()}"
        if identity in seen:
            continue
        seen.add(identity)
        items.append(
            {
                "channel": "wechat",
                "kind": "content",
                "evidence": "search_summary",
                "source": f"微信公众号（Agent Reach 检索摘要）｜{family}",
                "title": title,
                "summary": summary,
                "published_at": published,
                "observed_at": now.isoformat(timespec="seconds"),
                "url": _stable_wechat_url(row),
            }
        )
    relevant_count = len(items)
    items.sort(key=lambda item: _content_priority(item, now), reverse=True)
    diverse: list[dict[str, Any]] = []
    seen_families: set[str] = set()
    for item in items:
        family = str(item["source"]).rsplit("｜", 1)[-1]
        if family in seen_families:
            continue
        diverse.append(item)
        seen_families.add(family)
        if len(diverse) >= 2:
            break
    items = diverse
    status = "partial" if errors else "ok" if relevant_count else "no_results"
    summary = (
        (
            "已检索公众号公开索引；部分查询失败，已保留其余结果；仅保留标题、日期和摘要。"
            if errors
            else "已检索公众号公开索引；仅保留标题、日期和摘要，搜狗临时跳转链接未进入补充包。"
        )
        if items
        else (
            "公众号部分查询失败，其余查询没有通过时效性和相关性检查的内容。"
            if errors
            else "已检索公众号公开索引，本轮没有通过时效性和相关性检查的内容。"
        )
    )
    return _coverage(
        "wechat", status, summary, queries=len(WECHAT_QUERIES), raw_count=len(rows),
        relevant_count=relevant_count, detail_reads=0,
    ), items


def collect_agent_reach(
    *,
    now: datetime | None = None,
    runner: Callable[..., Any] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    coverage: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    try:
        xhs_coverage, xhs_items = _collect_xhs(current, runner, sleeper)
    except ChannelError as exc:
        xhs_coverage, xhs_items = _coverage(
            "xiaohongshu", exc.status,
            (
                "本机小红书检索未获取登录态；请在 OpenCLI 登录后重试。"
                if exc.status == "auth_required"
                else "本机小红书检索失败，不影响其他渠道。"
            ),
            queries=len(XHS_QUERIES),
        ), []
    coverage.append(xhs_coverage)
    items.extend(xhs_items)
    try:
        wechat_coverage, wechat_items = _collect_wechat(current, runner)
    except ChannelError:
        wechat_coverage, wechat_items = _coverage(
            "wechat", "error", "公众号公开索引检索失败，不影响其他渠道。",
            queries=len(WECHAT_QUERIES),
        ), []
    coverage.append(wechat_coverage)
    items.extend(wechat_items)
    coverage.extend(
        (
            _coverage(
                "maimai", "unsupported", "当前 OpenCLI 适配器不支持职位或职言搜索。"
            ),
            _coverage(
                "boss", "auth_required", "本采集器未读取并验证 BOSS 职位正文，未验活 JD 不输出。"
            ),
        )
    )
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "coverage": coverage,
        "items": items,
    }


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect a sanitized Agent Reach supplement")
    parser.add_argument("--output", type=Path, help="write JSON to this local file (mode 0600)")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    payload = collect_agent_reach()
    if args.output:
        _write_private(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
