"""Location buckets and conservative job-freshness rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

from radar_types import Assessment, Job


@dataclass(frozen=True)
class Freshness:
    label: str
    primary_eligible: bool
    trend_eligible: bool
    priority: int


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    chinese_date = re.fullmatch(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if chinese_date:
        return datetime(
            int(chinese_date.group(1)),
            int(chinese_date.group(2)),
            int(chinese_date.group(3)),
            tzinfo=timezone.utc,
        )
    localized_rfc = re.fullmatch(
        r"周.,\s*(\d{1,2})\s+(\d{1,2})月\s+(20\d{2})\s+"
        r"(\d{1,2}):(\d{2}):(\d{2})\s+GMT",
        text,
    )
    if localized_rfc:
        return datetime(
            int(localized_rfc.group(3)),
            int(localized_rfc.group(2)),
            int(localized_rfc.group(1)),
            int(localized_rfc.group(4)),
            int(localized_rfc.group(5)),
            int(localized_rfc.group(6)),
            tzinfo=timezone.utc,
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def job_freshness(job: Job, config: dict[str, Any], now: datetime | None = None) -> Freshness:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    preferred_days = int(config.get("preferred_job_age_days", 90))
    max_days = min(int(config.get("max_job_age_days", 180)), 180)

    deadline = parse_timestamp(job.valid_through)
    if deadline and deadline < now:
        return Freshness("申请截止日期已过", False, False, 9)
    if not job.active:
        return Freshness("未能确认当前仍在招聘", False, False, 9)

    posted = parse_timestamp(job.published_at)
    trusted_basis = job.date_basis in {"published", "created"}
    if posted and posted > now + timedelta(hours=48):
        posted = None
        trusted_basis = False
    if posted and trusted_basis:
        age = max(0, (now - posted).days)
        date_label = posted.date().isoformat()
        if age <= preferred_days:
            return Freshness(f"{date_label} 发布（{age}天）", True, True, 0)
        if age <= max_days:
            return Freshness(f"{date_label} 发布（{age}天，需再次确认）", True, True, 1)
        return Freshness(f"发布已超过 {max_days} 天", False, False, 9)

    if posted and job.date_basis == "updated" and job.official:
        age = max(0, (now - posted).days)
        date_label = posted.date().isoformat()
        if age <= preferred_days:
            return Freshness(f"官网更新于 {date_label}（非发布时间）", True, True, 1)
        if age <= max_days:
            return Freshness(f"官网 {age} 天前更新；仅作趋势参考", False, True, 2)
        return Freshness(f"官网更新时间已超过 {max_days} 天", False, False, 9)

    if job.official:
        return Freshness("发布日期未披露，官网当前在招；仅作趋势参考", False, True, 2)
    return Freshness("发布日期与当前状态均未核实", False, False, 9)


def location_bucket(job: Job, config: dict[str, Any]) -> str:
    location = job.location.strip()
    folded = location.casefold()
    unknown_terms = ("", "未披露", "unknown", "greater china", "全国远程", "remote china")
    if folded in {term.casefold() for term in unknown_terms}:
        return "unknown"
    if any(term.casefold() in folded for term in config.get("primary_locations", ())):
        return "primary"
    if job.scope == "global":
        return "global"
    if job.scope == "china":
        return "china_other"
    return "unknown"


def partition_market(
    items: Iterable[Assessment],
    config: dict[str, Any],
    now: datetime | None = None,
) -> tuple[list[Assessment], list[Assessment]]:
    primary: list[tuple[int, int, Assessment]] = []
    trends: list[tuple[int, int, Assessment]] = []
    for index, item in enumerate(items):
        freshness = job_freshness(item.job, config, now)
        bucket = location_bucket(item.job, config)
        if bucket == "primary" and freshness.primary_eligible:
            primary.append((freshness.priority, index, item))
        elif freshness.trend_eligible:
            trends.append((freshness.priority, index, item))
    primary.sort(key=lambda row: (row[0], row[1]))
    trends.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in primary], [row[2] for row in trends]
