#!/usr/bin/env python3
"""Daily official-job discovery, deterministic matching, and DingTalk delivery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Re-export the public helpers used by tests and local analysis.
from radar_ats import parse_ashby, parse_greenhouse, parse_moka
from radar_delivery import (
    format_report,
    load_seen,
    save_seen,
    send_dingtalk,
    signed_webhook_url,
    validate_dingtalk_webhook,
)
from radar_discovery import discover_jobs, enrich_jobs, parse_rss
from radar_matching import (
    assess_job,
    looks_like_product_job,
    parse_salary,
    rank_assessments,
    salary_gate,
    select_for_push,
)
from radar_types import Job


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
    return config


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
    jobs, failures = discover_jobs(config)
    jobs = [job for job in jobs if looks_like_product_job(job)]
    assessments = [assess_job(job, config) for job in jobs]
    candidates = [item.job for item in sorted(assessments, key=lambda item: item.fit, reverse=True)]
    enriched = enrich_jobs(candidates, int(config.get("max_detail_fetches", 10)))
    ranked = rank_assessments((assess_job(job, config) for job in enriched), config)

    state_was_initialized = args.state.exists()
    seen = load_seen(args.state)
    fresh = ranked if args.force_all else [item for item in ranked if item.job.identity not in seen]
    selected = select_for_push(
        fresh,
        max_jobs=int(config["max_push_jobs"]),
        max_global=int(config.get("max_global_push_jobs", 2)),
    )
    report = format_report(selected, len(jobs), failures)
    if args.dry_run:
        print(report)
        return 0

    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip()
    if not webhook or not secret:
        print("缺少 DINGTALK_WEBHOOK 或 DINGTALK_SECRET", file=sys.stderr)
        return 2
    send_dingtalk(report, webhook, secret)

    # Baseline all matches after the first successful delivery, preventing old
    # inventory from being dripped out as "new" over subsequent days.
    baseline = selected if state_was_initialized else ranked
    seen.update(item.job.identity for item in baseline)
    save_seen(args.state, seen)
    print(f"已推送 {len(selected)} 个新岗位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
