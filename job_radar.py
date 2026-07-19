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
from radar_ats import parse_ashby, parse_greenhouse, parse_moka, parse_tencent
from radar_delivery import (
    format_report,
    load_seen,
    load_seen_state,
    save_seen,
    save_seen_state,
    send_dingtalk,
    signed_webhook_url,
    validate_dingtalk_webhook,
)
from radar_discovery import discover_jobs, enrich_jobs, parse_rss
from radar_market import partition_market
from radar_matching import (
    assess_job,
    looks_like_product_job,
    parse_salary,
    rank_assessments,
    salary_gate,
    select_diverse_assessments,
    select_for_push,
)
from radar_trends import discover_trend_signals, parse_duckduckgo_lite, parse_trend_rss
from radar_types import Job, TrendSignal


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
) -> list[TrendSignal]:
    """Keep platform discovery broad while reserving room for career reports."""
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
    platform_quota = max_items - 1 if content and max_items > 1 else max_items
    selected: list[TrendSignal] = []
    selected_sources: set[str] = set()
    for signal in platform:
        if len(selected) >= platform_quota:
            break
        if signal.source not in selected_sources:
            selected.append(signal)
            selected_sources.add(signal.source)
    for signal in platform:
        if len(selected) >= platform_quota:
            break
        if signal not in selected:
            selected.append(signal)
    if content and len(selected) < max_items:
        selected.append(content[0])
    selected_ids = {signal.identity for signal in selected}
    for signal in signals:
        if len(selected) >= max_items:
            break
        if signal.identity not in selected_ids:
            selected.append(signal)
            selected_ids.add(signal.identity)
    return selected


def signals_to_baseline(
    signals: list[TrendSignal],
    selected: list[TrendSignal],
) -> list[TrendSignal]:
    selected_sources = {signal.source for signal in selected}
    return [signal for signal in signals if signal.source in selected_sources]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path(".cache/seen_jobs.json"))
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "").casefold() == "true")
    parser.add_argument("--force-all", action="store_true", default=os.getenv("FORCE_ALL", "").casefold() == "true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    signals, signal_failures = discover_trend_signals(config)
    jobs, failures = discover_jobs(config)
    jobs = [job for job in jobs if looks_like_product_job(job)]
    assessments = [assess_job(job, config) for job in jobs]
    candidates = [item.job for item in sorted(assessments, key=lambda item: item.fit, reverse=True)]
    enriched = enrich_jobs(
        candidates,
        int(config.get("max_detail_fetches", 10)),
        config.get("official_career_hosts", ()),
    )
    ranked = rank_assessments((assess_job(job, config) for job in enriched), config)

    primary_all, trend_all = partition_market(ranked, config)
    seen_state = load_seen_state(args.state, int(config.get("max_job_age_days", 180)))
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
    selected_signals = select_signals(
        fresh_signals,
        int(config.get("max_signal_push_items", 3)),
        set()
        if args.force_all
        else {
            identity.removeprefix("signal-source:")
            for identity in seen
            if identity.startswith("signal-source:")
        },
    )
    source_warnings = signal_failures[:1] + failures[:2] + signal_failures[1:] + failures[2:]
    report = format_report(
        selected_primary,
        len(jobs),
        source_warnings,
        trend_items=selected_trends,
        signals=selected_signals,
        config=config,
    )
    if args.dry_run:
        print(report)
        return 0

    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    if not webhook or not secret:
        print("缺少 DINGTALK_WEBHOOK 或 DINGTALK_SECRET", file=sys.stderr)
        return 2
    send_dingtalk(report, webhook, secret)

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    observed_identities = {
        *(f"primary:{item.job.identity}" for item in primary_all),
        *(f"trend:{item.job.identity}" for item in trend_all),
        *(
            f"signal:{signal.identity}"
            for signal in signals_to_baseline(signals, selected_signals)
        ),
        *(f"signal-source:{signal.source}" for signal in selected_signals),
    }
    seen_state.update((identity, observed_at) for identity in observed_identities)
    save_seen_state(args.state, seen_state)
    print(
        f"已推送 {len(selected_primary)} 个北京/天津岗位、"
        f"{len(selected_trends)} 个趋势岗位和 {len(selected_signals)} 条行业信号。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
