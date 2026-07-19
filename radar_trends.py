"""Public-search signals from job platforms, reports, and social content."""

from __future__ import annotations

import html
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from radar_ats import strip_html
from radar_discovery import bing_rss_url, http_get
from radar_market import parse_timestamp
from radar_matching import looks_like_candidate_job, parse_salary, salary_gate
from radar_search import duckduckgo_lite_url, parse_duckduckgo_results
from radar_types import Job, TrendSignal, is_public_http_url, is_stable_public_signal_url


AI_TERMS = (
    "agent", "agentic", "智能体", "大模型", "llm", "multimodal", "多模态",
    "eval", "benchmark", "评测", "可靠性", "ai product", "ai产品",
)
MARKET_TERMS = (
    "product manager", "product lead", "产品经理", "产品专家", "招聘", "岗位",
    "产品负责人", "高级", "资深", "负责人", "专家", "社招", "hiring", "career",
    "senior", "staff", "principal", "lead", "head", "职场", "薪酬", "人才", "报告", "趋势",
)


def parse_duckduckgo_lite(
    payload: bytes,
    source: str,
    kind: str,
    limit: int,
    observed_at: datetime | None = None,
) -> list[TrendSignal]:
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed_text = observed.astimezone(timezone.utc).isoformat(timespec="seconds")
    return [
        TrendSignal(result.title, result.url, result.summary, source, kind, observed_text)
        for result in parse_duckduckgo_results(payload, limit)
    ]


def parse_trend_rss(payload: bytes, source: str, kind: str, limit: int) -> list[TrendSignal]:
    root = ET.fromstring(payload)
    signals: list[TrendSignal] = []
    for item in root.findall(".//item")[:limit]:
        title = html.unescape((item.findtext("title") or "").strip())
        url = (item.findtext("link") or "").strip()
        summary = strip_html(item.findtext("description") or "")
        indexed_at = (item.findtext("pubDate") or "").strip()
        if title and is_public_http_url(url):
            signals.append(TrendSignal(title, url, summary, source, kind, indexed_at))
    return signals


def signal_is_recent(signal: TrendSignal, max_days: int, now: datetime | None = None) -> bool:
    indexed = parse_timestamp(signal.indexed_at)
    if not indexed:
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (now - indexed).days
    return -2 <= age <= max_days


def platform_signal_url_is_detail(url: str) -> bool:
    """Reject known platform aggregation pages while retaining actual job/detail URLs."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host in {"www.zhipin.com", "m.zhipin.com"}:
        return "/job_detail/" in path
    if host in {"www.liepin.com", "m.liepin.com"}:
        return "/job/" in path
    if host in {"www.zhaopin.com", "m.zhaopin.com"}:
        return "/jobdetail/" in path
    if host in {"www.linkedin.com", "cn.linkedin.com", "sg.linkedin.com"}:
        return "/jobs/view/" in path
    if host in {"cn.indeed.com", "www.indeed.com"}:
        return path == "/viewjob"
    return True


def signal_is_relevant(signal: TrendSignal, config: dict[str, Any] | None = None) -> bool:
    text = f"{signal.title}\n{signal.summary}".casefold()
    if signal.kind == "platform":
        job = Job(signal.title, signal.url, signal.summary, signal.source)
        if not platform_signal_url_is_detail(signal.url) or not looks_like_candidate_job(job):
            return False
        if config and salary_gate(parse_salary(job.text), config)[1]:
            return False
    return any(term.casefold() in text for term in AI_TERMS) and any(
        term.casefold() in text for term in MARKET_TERMS
    )


def signal_url_allowed(url: str, hosts: list[str]) -> bool:
    if not is_public_http_url(url):
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    return host in {item.casefold() for item in hosts}


def discover_trend_signals(config: dict[str, Any]) -> tuple[list[TrendSignal], list[str]]:
    discovered: dict[str, TrendSignal] = {}
    failures: list[str] = []
    limit = int(config.get("max_results_per_query", 12))
    max_age = int(config.get("trend_signal_max_age_days", 45))
    hosts = list(config.get("trend_signal_hosts", []))
    observed_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    configured_queries = list(config.get("trend_queries", []))
    content_queries = [query for query in configured_queries if query.get("kind") != "platform"]
    platform_queries = (
        [query for query in configured_queries if query.get("kind") == "platform"]
        if config.get("enable_public_platform_discovery", True)
        else []
    )
    ordered_queries: list[dict[str, Any]] = []
    for index in range(max(len(content_queries), len(platform_queries))):
        if index < len(content_queries):
            ordered_queries.append(content_queries[index])
        if index < len(platform_queries):
            ordered_queries.append(platform_queries[index])
    deadline = time.monotonic() + float(config.get("trend_search_budget_seconds", 240))
    for query in ordered_queries:
        if time.monotonic() >= deadline:
            failures.append("平台与行业信号：达到本轮时间预算，剩余查询本轮未覆盖")
            break
        signals: list[TrendSignal] = []
        primary_error: Exception | None = None
        try:
            payload = http_get(
                duckduckgo_lite_url(query["query"], query.get("language", "zh-CN")),
                timeout_seconds=6,
                attempts=1,
            )
            signals = parse_duckduckgo_lite(
                payload,
                query["name"],
                query.get("kind", "content"),
                limit,
            )
        except (KeyError, OSError, urllib.error.URLError) as exc:
            primary_error = exc

        accepted = [
            signal
            for signal in signals
            if signal_url_allowed(signal.url, hosts)
            and is_stable_public_signal_url(signal.url)
            and signal_is_relevant(signal, config)
            and signal_is_recent(signal, max_age)
        ]
        if not accepted:
            try:
                payload = http_get(
                    bing_rss_url(query["query"], query.get("language", "zh-CN")),
                    timeout_seconds=6,
                    attempts=1,
                )
                fallback = [
                    TrendSignal(
                        signal.title,
                        signal.url,
                        signal.summary,
                        signal.source,
                        signal.kind,
                        observed_text,
                    )
                    for signal in parse_trend_rss(
                        payload,
                        query["name"],
                        query.get("kind", "content"),
                        limit,
                    )
                ]
                accepted = [
                    signal
                    for signal in fallback
                    if signal_url_allowed(signal.url, hosts)
                    and is_stable_public_signal_url(signal.url)
                    and signal_is_relevant(signal, config)
                    and signal_is_recent(signal, max_age)
                ]
            except (ET.ParseError, KeyError, OSError, urllib.error.URLError) as exc:
                error = primary_error or exc
                failures.append(f"{query.get('name', 'trend')}: {type(error).__name__}")
        if not accepted and not any(
            failure.startswith(f"{query.get('name', 'trend')}:") for failure in failures
        ):
            failures.append(f"{query.get('name', 'trend')}: 未发现相关公开索引")
        for signal in accepted:
            discovered.setdefault(signal.identity, signal)
    return list(discovered.values()), failures
