#!/usr/bin/env python3
"""Daily official-job discovery, deterministic matching, and DingTalk delivery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-export the public helpers used by tests and local analysis.
from radar_ats import (
    parse_alibaba,
    parse_ashby,
    parse_bytedance,
    parse_greenhouse,
    parse_meituan,
    parse_moka,
    parse_tencent,
)
from radar_delivery import (
    format_action_report,
    format_market_report,
    format_report,
    load_seen,
    load_seen_state,
    save_seen,
    save_seen_state,
    send_dingtalk,
    signed_webhook_url,
    validate_dingtalk_webhook,
)
from radar_discovery import discover_jobs, discover_jobs_with_coverage, enrich_jobs, parse_rss
from radar_insights import (
    analyze_market,
    append_market_snapshot,
    build_market_snapshot,
    load_market_history,
    save_market_history,
)
from radar_market import parse_timestamp, partition_market
from radar_matching import (
    assess_job,
    looks_like_candidate_job,
    looks_like_product_job,
    parse_salary,
    rank_assessments,
    salary_gate,
    select_diverse_assessments,
    select_for_push,
)
from radar_supplement import (
    SupplementCoverage,
    SupplementValidationError,
    default_agent_reach_coverage,
    load_supplement,
)
from radar_trends import (
    discover_trend_signals,
    discover_trend_signals_with_coverage,
    parse_duckduckgo_lite,
    parse_trend_rss,
)
from radar_types import Job, TrendSignal


RECENT_PUSH_STATE_KEY = "run:last-successful-push"


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    private_numbers = {
        "CURRENT_FIXED_CASH_WAN": "current_fixed_cash_wan",
        "TARGET_TOTAL_COMP_WAN": "target_total_comp_wan",
    }
    for env_name, key in private_numbers.items():
        raw = os.getenv(env_name, "").strip()
        if raw:
            try:
                config[key] = float(raw)
            except ValueError as exc:
                raise ValueError(f"{env_name} 必须是正数") from exc
        if not isinstance(config.get(key), (int, float)) or float(config[key]) <= 0:
            raise ValueError(f"缺少私密配置 {env_name}")
    preferred_days = int(config.get("preferred_job_age_days", 90))
    max_days = int(config.get("max_job_age_days", 180))
    if not 1 <= preferred_days <= max_days <= 180:
        raise ValueError("岗位时效窗口必须满足 1 <= preferred <= max <= 180 天")
    return config


def was_seen(seen: set[str], bucket: str, identity: str) -> bool:
    if identity in seen:
        return True
    if bucket == "trend":
        return f"trend:{identity}" in seen or f"primary:{identity}" in seen
    return f"{bucket}:{identity}" in seen


def select_signals(
    signals: list[TrendSignal],
    max_items: int,
    seen_sources: set[str] | None = None,
    *,
    platform_limit: int | None = None,
    content_limit: int | None = None,
) -> list[TrendSignal]:
    """Select broad evidence; each social channel may contribute at most two items."""
    if max_items <= 0:
        return []
    seen_sources = seen_sources or set()

    def prioritize_unseen_sources(values: list[TrendSignal]) -> list[TrendSignal]:
        return [value for value in values if value.source not in seen_sources] + [
            value for value in values if value.source in seen_sources
        ]

    platform = prioritize_unseen_sources(
        [signal for signal in signals if signal.kind == "platform"]
    )
    content = prioritize_unseen_sources(
        [signal for signal in signals if signal.kind != "platform"]
    )

    def social_family(signal: TrendSignal) -> str:
        if signal.source.startswith("小红书"):
            return "xiaohongshu"
        if signal.source.startswith("微信公众号"):
            return "wechat"
        return ""

    def diverse(values: list[TrendSignal], limit: int) -> list[TrendSignal]:
        chosen: list[TrendSignal] = []
        chosen_ids: set[str] = set()
        chosen_sources: set[str] = set()
        social_family_counts: dict[str, int] = {}
        for signal in values:
            if signal.identity in chosen_ids:
                continue
            family = social_family(signal)
            if family and social_family_counts.get(family, 0) >= 2:
                continue
            if not family and signal.source in chosen_sources:
                continue
            chosen.append(signal)
            chosen_ids.add(signal.identity)
            chosen_sources.add(signal.source)
            if family:
                social_family_counts[family] = social_family_counts.get(family, 0) + 1
            if len(chosen) >= limit:
                break
        return chosen

    if platform_limit is not None or content_limit is not None:
        platform_quota = max(0, platform_limit or 0)
        content_quota = max(0, content_limit or 0)
        return (
            diverse(platform, platform_quota) + diverse(content, content_quota)
        )[:max_items]

    platform_quota = max_items - 1 if content and max_items > 1 else max_items
    selected: list[TrendSignal] = []
    selected_ids: set[str] = set()
    selected_sources: set[str] = set()
    selected_social_families: dict[str, int] = {}

    def add_if_diverse(signal: TrendSignal) -> bool:
        if signal.identity in selected_ids:
            return False
        family = social_family(signal)
        if family and selected_social_families.get(family, 0) >= 2:
            return False
        if not family and signal.source in selected_sources:
            return False
        selected.append(signal)
        selected_ids.add(signal.identity)
        selected_sources.add(signal.source)
        if family:
            selected_social_families[family] = selected_social_families.get(family, 0) + 1
        return True

    for signal in platform:
        if len(selected) >= platform_quota:
            break
        add_if_diverse(signal)
    if content and len(selected) < max_items:
        add_if_diverse(content[0])
    for signal in signals:
        if len(selected) >= max_items:
            break
        add_if_diverse(signal)
    return selected


def signals_to_baseline(
    signals: list[TrendSignal],
    selected: list[TrendSignal],
) -> list[TrendSignal]:
    selected_sources = {signal.source for signal in selected}
    return [signal for signal in signals if signal.source in selected_sources]


def delivered_seen_identities(
    selected_primary: list[Assessment],
    selected_trends: list[Assessment],
    signals: list[TrendSignal],
    selected_signals: list[TrendSignal],
) -> set[str]:
    """Persist only jobs the user actually received; keep unshown jobs in rotation."""
    return {
        *(f"primary:{item.job.identity}" for item in selected_primary),
        *(f"trend:{item.job.identity}" for item in selected_trends),
        *(
            f"signal:{signal.identity}"
            for signal in signals_to_baseline(signals, selected_signals)
        ),
        *(f"signal-source:{signal.source}" for signal in selected_signals),
    }


def merge_signals(
    public_signals: list[TrendSignal],
    supplement_signals: tuple[TrendSignal, ...] | list[TrendSignal],
) -> list[TrendSignal]:
    """Merge evidence with locally validated content taking precedence."""
    local = list(supplement_signals)
    local_identities = {signal.identity for signal in local}
    local_titles = {" ".join(signal.title.casefold().split()) for signal in local}
    merged = local + [
        signal
        for signal in public_signals
        if signal.identity not in local_identities
        and " ".join(signal.title.casefold().split()) not in local_titles
    ]
    return merged


def failed_agent_reach_coverage(reason: str) -> tuple[SupplementCoverage, ...]:
    compact = " ".join(reason.split())[:120]
    return (
        SupplementCoverage("xiaohongshu", "error", compact),
        SupplementCoverage("wechat", "error", compact),
        SupplementCoverage("maimai", "unsupported", "当前适配器不支持职位或职言搜索"),
        SupplementCoverage("boss", "auth_required", "未验证本机 BOSS 登录态；未实读 JD 不推送"),
    )


def pushed_within(
    seen_state: dict[str, str],
    hours: float,
    now: datetime | None = None,
) -> bool:
    if hours <= 0:
        return False
    timestamp = parse_timestamp(seen_state.get(RECENT_PUSH_STATE_KEY))
    if not timestamp:
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (current - timestamp).total_seconds()
    return -300 <= age_seconds <= hours * 3600


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path(".cache/seen_jobs.json"))
    parser.add_argument(
        "--market-history",
        type=Path,
        default=Path(".cache/market_history.json"),
        help="daily aggregate snapshots used only after a successful two-message push",
    )
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "").casefold() == "true")
    parser.add_argument("--force-all", action="store_true", default=os.getenv("FORCE_ALL", "").casefold() == "true")
    supplement_default = os.getenv("AGENT_REACH_SUPPLEMENT_PATH", "").strip()
    parser.add_argument(
        "--supplement",
        type=Path,
        default=Path(supplement_default) if supplement_default else None,
        help="sanitized local Agent Reach JSON supplement",
    )
    parser.add_argument(
        "--require-supplement",
        action="store_true",
        default=os.getenv("REQUIRE_AGENT_REACH_SUPPLEMENT", "").casefold() == "true",
        help="fail closed when the requested supplement is absent or invalid",
    )
    parser.add_argument(
        "--skip-if-recent-push-hours",
        type=float,
        default=float(os.getenv("SKIP_IF_RECENT_PUSH_HOURS", "0") or 0),
        help="cloud-schedule fallback: skip when another push just succeeded",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    seen_state = load_seen_state(args.state, int(config.get("max_job_age_days", 180)))
    if not args.force_all and pushed_within(seen_state, args.skip_if_recent_push_hours):
        print("近期已有一次成功推送；本轮云端定时任务作为兜底跳过。")
        return 0
    supplement_signals: tuple[TrendSignal, ...] = ()
    supplement_coverage = default_agent_reach_coverage()
    supplement_failure = ""
    if args.supplement:
        try:
            supplement = load_supplement(args.supplement)
        except SupplementValidationError as exc:
            supplement_failure = f"Agent Reach 补充包无效：{exc}"
            if args.require_supplement:
                print(supplement_failure, file=sys.stderr)
                return 2
            supplement_coverage = failed_agent_reach_coverage(supplement_failure)
        else:
            supplement_signals = supplement.signals
            supplement_coverage = supplement.coverage
    elif args.require_supplement:
        print("要求 Agent Reach 补充包，但没有提供 --supplement", file=sys.stderr)
        return 2

    signals, signal_failures, trend_query_coverage = discover_trend_signals_with_coverage(config)
    signals = merge_signals(signals, supplement_signals)
    jobs, failures, official_coverage = discover_jobs_with_coverage(config)
    jobs = [job for job in jobs if looks_like_candidate_job(job)]
    assessments = [assess_job(job, config) for job in jobs]
    candidates = [item.job for item in sorted(assessments, key=lambda item: item.fit, reverse=True)]
    enriched = enrich_jobs(
        candidates,
        int(config.get("max_detail_fetches", 10)),
        config.get("employer_career_hosts", config.get("official_career_hosts", ())),
    )
    enriched_assessments = [assess_job(job, config) for job in enriched]
    ranked = rank_assessments(enriched_assessments, config)

    market_snapshot = build_market_snapshot(
        enriched_assessments,
        official_coverage,
        config,
    )
    market_history = load_market_history(args.market_history)
    market_insight = analyze_market(market_snapshot, market_history)

    primary_all, trend_all = partition_market(ranked, config)
    seen = set(seen_state)
    if args.force_all:
        fresh_primary, fresh_trends, fresh_signals = primary_all, trend_all, signals
    else:
        fresh_primary = [item for item in primary_all if not was_seen(seen, "primary", item.job.identity)]
        fresh_trends = [item for item in trend_all if not was_seen(seen, "trend", item.job.identity)]
        fresh_signals = [signal for signal in signals if not was_seen(seen, "signal", signal.identity)]
    selected_primary = select_diverse_assessments(
        fresh_primary,
        int(config.get("max_primary_push_jobs", 4)),
    )
    selected_trends = select_diverse_assessments(
        fresh_trends,
        int(config.get("max_trend_push_jobs", 3)),
    )
    platform_signal_limit = int(config.get("max_platform_signal_items", 0))
    content_signal_limit = int(config.get("max_content_signal_items", 0))
    signal_limit = (
        platform_signal_limit + content_signal_limit
        if platform_signal_limit or content_signal_limit
        else int(config.get("max_signal_push_items", 3))
    )
    selected_signals = select_signals(
        fresh_signals,
        signal_limit,
        set()
        if args.force_all
        else {
            identity.removeprefix("signal-source:")
            for identity in seen
            if identity.startswith("signal-source:")
        },
        platform_limit=platform_signal_limit or None,
        content_limit=content_signal_limit or None,
    )
    source_warnings = (
        ([supplement_failure] if supplement_failure else [])
        + signal_failures[:1]
        + failures[:2]
        + signal_failures[1:]
        + failures[2:]
    )
    action_report = format_action_report(
        selected_primary,
        len(jobs),
        source_warnings,
        trend_items=selected_trends,
        config=config,
    )
    market_report = format_market_report(
        market_snapshot,
        market_insight,
        official_coverage=official_coverage,
        trend_coverage=trend_query_coverage,
        signals=selected_signals,
        evidence_signals=signals,
        source_coverage=supplement_coverage,
        failures=source_warnings,
        config=config,
    )
    if args.dry_run:
        print(action_report)
        print("\n\n--- \u7b2c 2 条钉钉消息 ---\n")
        print(market_report)
        return 0

    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    if not webhook or not secret:
        print("缺少 DINGTALK_WEBHOOK 或 DINGTALK_SECRET", file=sys.stderr)
        return 2
    send_dingtalk(action_report, webhook, secret, title="Sunny 岗位机会")
    send_dingtalk(market_report, webhook, secret, title="Sunny AI 求职市场情报")

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    observed_identities = delivered_seen_identities(
        selected_primary,
        selected_trends,
        signals,
        selected_signals,
    )
    seen_state.update((identity, observed_at) for identity in observed_identities)
    seen_state[RECENT_PUSH_STATE_KEY] = observed_at
    save_seen_state(args.state, seen_state)
    save_market_history(
        args.market_history,
        append_market_snapshot(market_history, market_snapshot),
    )
    print(
        f"已推送 {len(selected_primary)} 个北京/天津岗位、"
        f"{len(selected_trends)} 个趋势岗位和 {len(selected_signals)} 条行业信号。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
