"""State persistence, DingTalk-safe formatting, signing, and delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from radar_market import job_freshness, parse_timestamp
from radar_supplement import SupplementCoverage
from radar_types import (
    HTTP_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    USER_AGENT,
    Assessment,
    TrendSignal,
    is_public_http_url,
    is_stable_public_signal_url,
)


def load_seen_state(
    path: Path,
    retention_days: int = 180,
    now: datetime | None = None,
) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=min(max(retention_days, 1), 180))
    raw_jobs = data.get("jobs")
    if isinstance(raw_jobs, dict):
        raw_entries = raw_jobs.items()
    else:
        migrated_at = str(data.get("updated_at") or now.isoformat())
        raw_entries = ((identity, migrated_at) for identity in data.get("job_ids", []))
    entries: dict[str, str] = {}
    for identity, raw_value in raw_entries:
        value = raw_value.get("last_seen_at") if isinstance(raw_value, dict) else raw_value
        last_seen = parse_timestamp(value)
        if isinstance(identity, str) and last_seen and last_seen >= cutoff:
            entries[identity] = last_seen.isoformat()
    return entries


def save_seen_state(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "jobs": dict(sorted(entries.items())),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_seen(path: Path) -> set[str]:
    return set(load_seen_state(path))


def save_seen(path: Path, seen: set[str]) -> None:
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_seen_state(path, {identity: observed for identity in seen})


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = "\n\n内容过长，已在完整行边界截断。"
    budget = max_bytes - len(suffix.encode("utf-8"))
    kept: list[str] = []
    used = 0
    for line in value.splitlines(keepends=True):
        size = len(line.encode("utf-8"))
        if used + size > budget:
            break
        kept.append(line)
        used += size
    return "".join(kept).rstrip() + suffix


def signal_excerpt(signal: TrendSignal, max_chars: int = 90) -> str:
    compact = " ".join(signal.summary.split())
    return compact[:max_chars] + ("…" if len(compact) > max_chars else "")


def markdown_text(value: Any) -> str:
    compact = " ".join(str(value).split())
    escaped = compact.replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "(", ")", "<", ">", "|"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


def markdown_link(label: Any, url: str) -> str:
    safe_label = markdown_text(label)
    if not is_public_http_url(url):
        return safe_label
    safe_url = urllib.parse.quote(url, safe=":/?#[]@!$&'*+,;=%")
    return f"[{safe_label}]({safe_url})"


def signal_title_search_url(signal: TrendSignal) -> str:
    """Build a token-free platform search URL when a stable article URL is unavailable."""
    title = " ".join(signal.title.split()).strip()
    if not title:
        return ""
    source = signal.source.casefold()
    if "小红书" in source:
        return "https://www.xiaohongshu.com/search_result?" + urllib.parse.urlencode(
            {"keyword": title}
        )
    if "微信" in source or "公众号" in source:
        return "https://weixin.sogou.com/weixin?" + urllib.parse.urlencode(
            {"type": "2", "query": title}
        )
    return ""


COVERAGE_LABELS = {
    "boss": "BOSS 直聘",
    "liepin": "猎聘",
    "linkedin": "LinkedIn",
    "maimai": "脉脉",
    "wechat": "微信公众号",
    "xiaohongshu": "小红书",
}
COVERAGE_STATUSES = {
    "ok": "已执行",
    "partial": "部分成功",
    "no_results": "已执行，无合格结果",
    "auth_required": "需登录 / 未验活",
    "unsupported": "当前不支持",
    "error": "执行失败",
    "skipped": "本轮未执行",
    "skipped_disabled": "本轮未执行",
    "skipped_budget": "达到时间预算未执行",
}

PLATFORM_COVERAGE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("猎聘", ("猎聘", "liepin")),
    ("51job / 前程无忧", ("51job", "前程无忧")),
    ("智联招聘", ("智联", "zhaopin")),
    ("LinkedIn", ("linkedin",)),
    ("Indeed", ("indeed",)),
    ("国聘", ("国聘", "iguopin")),
    (
        "就业在线 / 公共就业 / 人才网",
        (
            "就业在线",
            "人才网",
            "公共就业",
            "公共招聘",
            "jobonline",
            "tjrc",
            "tjtalents",
            "newjobs",
        ),
    ),
    ("BOSS 直聘", ("boss", "zhipin")),
    ("脉脉", ("脉脉", "maimai")),
)


def format_source_coverage(rows: list[SupplementCoverage] | tuple[SupplementCoverage, ...]) -> list[str]:
    lines = ["## Agent Reach 实际覆盖", ""]
    for row in rows:
        counters = (
            f"检索 {row.queries} 次 / 原始 {row.raw_count} 条 / "
            f"相关 {row.relevant_count} 条 / 正文实读 {row.detail_reads} 条"
        )
        summary = f"｜{markdown_text(row.summary)}" if row.summary else ""
        lines.append(
            f"- **{COVERAGE_LABELS.get(row.channel, row.channel)}：** "
            f"{COVERAGE_STATUSES.get(row.status, row.status)}｜{counters}{summary}"
        )
    lines.append("")
    return lines


def _value(value: Any, *names: str, default: Any = None) -> Any:
    """Read a field from either a dataclass-like object or a mapping."""
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if value is not None and hasattr(value, name):
            return getattr(value, name)
    return default


def _compact(value: Any, max_chars: int = 80) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _first(values: Sequence[Any] | None, fallback: str = "未披露", max_chars: int = 72) -> str:
    if not values:
        return fallback
    return _compact(values[0], max_chars)


def _joined(values: Sequence[Any] | None, fallback: str, limit: int = 2, max_chars: int = 72) -> str:
    if not values:
        return fallback
    return _compact("；".join(str(value) for value in list(values)[:limit]), max_chars)


def _ownership_signal(item: Assessment) -> str:
    text = item.job.text.casefold()
    explicit_ic = any(
        term in text
        for term in ("individual contributor", "role type: individual contributor", "个人贡献者", "高级 ic")
    )
    ownership = any(
        term in text
        for term in ("ownership", "own ", "end-to-end", "负责", "主导", "定义", "路线图", "roadmap")
    )
    if explicit_ic and ownership:
        return "明确 IC，且有任务/路线 ownership 文本；最终决策边界仍需核验"
    if explicit_ic:
        return "明确 IC；任务决策权与指标归属需核验"
    if ownership:
        return "有 ownership 文本；需确认不是协调税、是否拥有验收与取舍权"
    return "IC/管理边界及关键决策权均未披露"


def _signal_dates(signal: TrendSignal, timezone: ZoneInfo) -> str:
    indexed = parse_timestamp(signal.indexed_at)
    published = parse_timestamp(signal.published_at)
    observed_label = indexed.astimezone(timezone).date().isoformat() if indexed else "发现日期未知"
    published_label = (
        f"原文日期 {published.astimezone(timezone).date().isoformat()}"
        if published
        else "原文日期未披露"
    )
    return f"本次发现 {observed_label}｜{published_label}"


def _metric_value(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _format_distribution(value: Any, denominator: int, limit: int = 5) -> str:
    if not isinstance(value, Mapping) or denominator <= 0:
        return "样本不足"
    ranked = sorted(
        (
            (str(name), count)
            for name, raw_count in value.items()
            if (count := _metric_value(raw_count)) is not None and count > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )[:limit]
    if not ranked:
        return "未观测到明确信号"
    return "；".join(
        f"{markdown_text(name)} {count:g}/{denominator} ({count / denominator:.0%})"
        for name, count in ranked
    )


def _official_coverage_summary(
    rows: Sequence[Any] | None,
    snapshot: Any,
) -> list[str]:
    planned = int(_value(snapshot, "official_sources_planned", default=0) or 0)
    healthy = int(_value(snapshot, "official_sources_ok", default=0) or 0)
    if rows:
        planned = planned or len(rows)
        statuses = [str(_value(row, "status", default="")) for row in rows]
        healthy = healthy or sum(status in {"ok", "no_results"} for status in statuses)
    if not planned:
        return ["- **企业官方源：** 本轮未提供健康度数据"]
    lines = [f"- **企业官方源：** 正常 {healthy}/{planned}"]
    decision_planned = int(_value(snapshot, "decision_sources_planned", default=0) or 0)
    decision_healthy = int(_value(snapshot, "decision_sources_ok", default=0) or 0)
    if decision_planned:
        lines.append(
            f"- **中国求职决策源：** 正常 {decision_healthy}/{decision_planned}；"
            "北京/天津时机只用该口径，不由海外源健康度兜底"
        )
    productive: list[str] = []
    unhealthy: list[str] = []
    for row in rows or ():
        status = str(_value(row, "status", default=""))
        label = _value(row, "label", "name", "source", "source_key", default="未知来源")
        count = int(_value(row, "count", "result_count", default=0) or 0)
        if count > 0:
            productive.append(f"{markdown_text(label)} {count} 条")
        if status in {"ok", "no_results"}:
            continue
        unhealthy.append(f"{markdown_text(label)}（{markdown_text(COVERAGE_STATUSES.get(status, status or '异常'))}）")
    if productive:
        suffix = "；".join(productive[:6])
        if len(productive) > 6:
            suffix += f"；其他 {len(productive) - 6} 个源"
        lines.append(f"- **官网返回标题初筛候选：** {suffix}")
    if unhealthy:
        suffix = "；".join(unhealthy[:4])
        if len(unhealthy) > 4:
            suffix += f"；其他 {len(unhealthy) - 4} 个"
        lines.append(f"- **待恢复来源：** {suffix}")
    return lines


def _platform_coverage_family(name: str) -> str:
    folded = name.casefold()
    for label, aliases in PLATFORM_COVERAGE_FAMILIES:
        if any(alias.casefold() in folded for alias in aliases):
            return label
    return "其他招聘平台"


def _trend_coverage_summary(
    rows: Sequence[Any] | None,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Report attempted public searches; never equate configured with executed."""
    platform_rows = [
        row
        for row in rows or ()
        if str(_value(row, "kind", default="platform")) == "platform"
    ]
    if not platform_rows:
        return ["- **招聘平台公开检索：** 本轮未提供逐查询执行记录"]

    groups: dict[str, list[Any]] = {}
    for row in platform_rows:
        name = str(_value(row, "name", "source", default="未知平台"))
        groups.setdefault(_platform_coverage_family(name), []).append(row)

    order = [label for label, _ in PLATFORM_COVERAGE_FAMILIES]
    if "其他招聘平台" in groups:
        order.append("其他招聘平台")
    disabled_reason = str(
        (config or {}).get(
            "platform_discovery_disabled_reason",
            "公开检索关闭 / 无可靠正文验活",
        )
    )
    lines = [
        "- **招聘平台公开检索实况：** 以实际发起的查询计数；写入配置不等于已执行"
    ]
    liepin_executed = 0
    liepin_succeeded = 0
    liepin_failed = 0
    liepin_accepted = 0
    for label in order:
        family_rows = groups.get(label)
        if not family_rows:
            continue
        statuses = [str(_value(row, "status", default="")) for row in family_rows]
        executed_rows = [
            row
            for row, status in zip(family_rows, statuses)
            if not status.startswith("skipped")
        ]
        skipped_statuses = [status for status in statuses if status.startswith("skipped")]
        raw_count = sum(
            int(_value(row, "raw_count", default=0) or 0) for row in executed_rows
        )
        accepted = sum(
            int(_value(row, "accepted_count", default=0) or 0) for row in executed_rows
        )
        failed = sum(status == "error" for status in statuses)
        executed = len(executed_rows)
        succeeded = executed - failed
        total = len(family_rows)

        if not executed:
            if any(status == "skipped_budget" for status in skipped_statuses):
                reason = "达到本轮时间预算"
            else:
                reason = disabled_reason
            lines.append(
                f"- **{markdown_text(label)}：** 本轮未执行（原因：{markdown_text(reason)}）｜"
                f"实际执行 0/{total} 组｜原始 0 条｜合格 L3 0 条"
            )
        else:
            suffix = ""
            if skipped_statuses:
                reasons: list[str] = []
                if any(status in {"skipped", "skipped_disabled"} for status in skipped_statuses):
                    reasons.append(disabled_reason)
                if any(status == "skipped_budget" for status in skipped_statuses):
                    reasons.append("达到本轮时间预算")
                suffix += (
                    f"｜未执行 {len(skipped_statuses)} 组"
                    f"（原因：{markdown_text('；'.join(reasons) or '未记录')}）"
                )
            lines.append(
                f"- **{markdown_text(label)}：** 实际执行 {executed}/{total} 组"
                f"（成功 {succeeded} / 失败 {failed}）｜"
                f"原始 {raw_count} 条（可含跨引擎重复）｜逐查询合格 L3 {accepted} 条（可跨查询重复）{suffix}"
            )
        if label == "猎聘":
            liepin_executed = executed
            liepin_succeeded = succeeded
            liepin_failed = failed
            liepin_accepted = accepted

    if "猎聘" in groups:
        lines.append(
            "- **猎聘口径：** 不做定时直接爬取；仅从公开搜索索引接受 "
            "`/job/<数字>.shtml` 或 `/a/<数字>.shtml` 直达页，仍需打开确认在招状态。"
        )
        if liepin_succeeded and not liepin_accepted:
            lines.append(
                "- **猎聘本轮结果：** 成功完成的查询中无合格 L3 线索；泛化 SEO 页和低于薪酬红线的结果未输出。"
            )
        elif liepin_executed and liepin_failed == liepin_executed:
            lines.append(
                "- **猎聘本轮结果：** 查询已发起但全部失败；不能据此推断“无合格岗位”。"
            )
    return lines


def _content_coverage_summary(rows: Sequence[Any] | None) -> list[str]:
    content_rows = [
        row
        for row in rows or ()
        if str(_value(row, "kind", default="platform")) != "platform"
    ]
    if not content_rows:
        return ["- **公开内容搜索实况：** 本轮未提供逐查询执行记录"]

    def family(row: Any) -> str:
        name = str(_value(row, "name", "source", default=""))
        if "微信" in name or "公众号" in name:
            return "公众号公开索引"
        if any(term in name for term in ("人社", "就业", "政府", "招聘专项")):
            return "政府 / 公共就业内容"
        return "行业媒体 / 报告"

    grouped: dict[str, list[Any]] = {}
    for row in content_rows:
        grouped.setdefault(family(row), []).append(row)
    lines = ["- **公开内容搜索实况：** 配置、执行、失败和入选 L4 分开计数"]
    for label in ("公众号公开索引", "政府 / 公共就业内容", "行业媒体 / 报告"):
        values = grouped.get(label, [])
        if not values:
            continue
        statuses = [str(_value(row, "status", default="")) for row in values]
        executed_rows = [
            row for row, status in zip(values, statuses) if not status.startswith("skipped")
        ]
        failed = sum(status == "error" for status in statuses)
        raw = sum(int(_value(row, "raw_count", default=0) or 0) for row in executed_rows)
        accepted = sum(int(_value(row, "accepted_count", default=0) or 0) for row in executed_rows)
        skipped = len(values) - len(executed_rows)
        suffix = f"｜未执行 {skipped}" if skipped else ""
        lines.append(
            f"- **{label}：** 实际执行 {len(executed_rows)}/{len(values)} 组"
            f"（成功 {len(executed_rows) - failed} / 失败 {failed}）｜原始 {raw} 条｜"
            f"逐查询合格 L4 {accepted} 条（可跨查询重复）{suffix}"
        )
    return lines


def _market_segments(snapshot: Any) -> dict[str, Mapping[str, Any]]:
    raw = _value(snapshot, "segments", default={})
    if isinstance(raw, Mapping) and any(isinstance(value, Mapping) for value in raw.values()):
        return {
            str(label): value
            for label, value in raw.items()
            if isinstance(value, Mapping)
        }
    # Backward compatibility for callers that still provide the original flat snapshot.
    return {
        "北京/天津": {
            "sample_count": int(_value(snapshot, "sample_count", default=0) or 0),
            "directions": _value(snapshot, "directions", default={}),
            "skills": _value(snapshot, "skills", default={}),
            "freshness": _value(snapshot, "freshness", default={}),
            "salary_disclosed_count": int(_value(snapshot, "salary_disclosed_count", default=0) or 0),
            "work_boundary_signal_count": int(_value(snapshot, "work_boundary_signal_count", default=0) or 0),
            "published_date_known_count": 0,
            "new_postings_7d": 0,
            "previous_postings_7d": 0,
            "new_postings_28d": 0,
        }
    }


def _content_corroboration(signals: Sequence[TrendSignal], now: datetime, max_age_days: int) -> str:
    recent: list[TrendSignal] = []
    for signal in signals:
        published = parse_timestamp(signal.published_at)
        if published and timedelta(0) <= now.astimezone(timezone.utc) - published <= timedelta(days=max_age_days):
            recent.append(signal)
    if not signals:
        return "本轮没有 L4 内容证据"
    themes = (
        ("评测/可靠性", ("eval", "评测", "benchmark", "可靠", "幻觉", "安全")),
        ("Builder 技术门槛", ("python", "api", "mcp", "tool calling", "原型", "trace")),
        ("高阶社招", ("社招", "高级", "资深", "负责人", "lead", "principal", "staff")),
        ("薪酬/人才流动", ("薪酬", "总包", "人才流动", "招聘趋势", "人才画像")),
    )
    counts: list[str] = []
    for label, terms in themes:
        count = sum(
            any(term.casefold() in f"{signal.title}\n{signal.summary}".casefold() for term in terms)
            for signal in recent
        )
        if count:
            counts.append(f"{label} {count}/{len(recent)}")
    theme_text = "；".join(counts) if counts else "近期样本未形成重复主题"
    return (
        f"可确认原始发布日期且在 {max_age_days} 天内 {len(recent)}/{len(signals)}；{theme_text}。"
        "发布日期未知或过旧的内容仅作背景，不参与时机判断"
    )


def format_action_report(
    items: list[Assessment],
    discovered_count: int,
    failures: list[str],
    *,
    trend_items: list[Assessment] | None = None,
    platform_items: list[Assessment] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Render a concise, click-first report containing only job actions."""
    config = config or {}
    trend_items = trend_items or []
    platform_items = platform_items or []
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    now = datetime.now(timezone)
    lines = [
        f"## Sunny 每日 Agent 岗位雷达·可行动岗位｜{now:%Y-%m-%d}",
        "",
        "## 北京 / 天津｜社招优先可行动岗位",
        "",
    ]
    if not items:
        lines.extend(["今天没有发现新的、通过时效与职业红线的北京/天津岗位。", ""])
    detailed_items = items[:3]
    compact_items = items[3:]
    for index, item in enumerate(detailed_items, 1):
        decision = "强匹配" if item.fit >= 85 and item.ready >= 65 else "值得核验"
        freshness = job_freshness(item.job, config, now)
        company = item.job.company or item.job.source
        cta_label = "查看企业官网 JD →" if item.job.official else "查看岗位详情 →"
        lines.extend(
            [
                f"### {index}. 【{decision}】{markdown_text(item.job.title)}",
                f"- **公司 / 地点：** {markdown_text(company)}｜{markdown_text(item.job.location)}｜{markdown_text(item.job.source)}",
                f"- **岗位时效：** {markdown_text(freshness.label)}",
                f"- **核心任务：** {markdown_text(_joined(item.responsibilities, 'JD 正文未形成明确任务标签', max_chars=70))}",
                f"- **权限判断：** {markdown_text(_compact(_ownership_signal(item), 70))}",
                f"- **匹配：** Fit / Ready {item.fit} / {item.ready}｜**两年资产：** {markdown_text(_compact(item.asset, 54))}",
                f"- **可迁移优势：** {markdown_text(_joined(item.strengths, '复杂问题定义与跨团队落地', max_chars=64))}",
                f"- **关键缺口：** {markdown_text(_joined(item.gaps, '需面试核验硬门槛', max_chars=70))}",
                f"- **薪酬：** {markdown_text(_compact(item.salary.label, 32))}；{markdown_text(_compact(item.salary_gate, 54))}",
                f"- **强度 / 差旅：** {markdown_text(_compact(item.work_risk, 68))}",
                markdown_link(cta_label, item.job.url),
                "",
            ]
        )

    if compact_items:
        lines.extend(["## 补充短名单｜建议二次核验", ""])
        for index, item in enumerate(compact_items, len(detailed_items) + 1):
            freshness = job_freshness(item.job, config, now)
            company = item.job.company or item.job.source
            lines.extend(
                [
                    f"### {index}. {markdown_text(item.job.title)}",
                    f"- **公司 / 地点 / 时效：** {markdown_text(company)}｜{markdown_text(item.job.location)}｜{markdown_text(freshness.label)}",
                    f"- **匹配 / 资产：** Fit/Ready {item.fit}/{item.ready}｜{markdown_text(_compact(item.asset, 48))}",
                    f"- **先核验：** {markdown_text(_joined(item.gaps, '薪酬、权限和生活边界', max_chars=62))}",
                    markdown_link("查看岗位详情 →", item.job.url),
                    "",
                ]
            )

    if platform_items:
        lines.extend(["## 平台岗位｜BOSS / 猎聘（本机已验活 L2）", ""])
        for item in platform_items:
            company = item.job.company or item.job.source
            lines.extend(
                [
                    f"- **{markdown_text(item.job.title)}**｜{markdown_text(company)}｜"
                    f"{markdown_text(item.job.location)}｜{markdown_text(item.job.source)}",
                    f"  Fit/Ready {item.fit}/{item.ready}｜薪酬：{markdown_text(_compact(item.salary.label, 40))}｜"
                    "时效：本轮实读在招，发布日期未披露",
                    f"  平台信息以企业官网与面试确认为准；先核验：{markdown_text(_joined(item.gaps, '薪酬、权限与生活边界', limit=1, max_chars=56))}",
                    markdown_link("查看平台岗位详情 →", item.job.url),
                ]
            )
        lines.append("")
    if trend_items:
        lines.extend(["## 行业参考岗位｜其他城市 / 海外", ""])
        for item in trend_items:
            freshness = job_freshness(item.job, config, now)
            lines.extend(
                [
                    f"- **{markdown_text(item.job.title)}**｜{markdown_text(item.job.location)}｜"
                    f"Fit/Ready {item.fit}/{item.ready}｜{markdown_text(freshness.label)}",
                    markdown_link("查看岗位详情 →", item.job.url),
                ]
            )
        lines.append("")
    lines.append(
        f"本轮发现层符合目标标题的候选 {discovered_count} 条（含全球趋势，不等同于可投递）；"
        f"通过正文、地点、时效和职业红线后主推 {len(items)} 条。"
    )
    if failures:
        lines.append(f"来源异常 {len(failures)} 项，详见《市场情报》的源健康度。")
    lines.append("薪酬、双休、21 点后工作频率和差旅以招聘方书面确认及反向背调为准。")
    return truncate_utf8("\n".join(lines), MAX_MESSAGE_BYTES)


def format_market_report(
    snapshot: Any = None,
    insight: Any = None,
    *,
    official_coverage: Sequence[Any] | None = None,
    trend_coverage: Sequence[Any] | None = None,
    signals: Sequence[TrendSignal] | None = None,
    evidence_signals: Sequence[TrendSignal] | None = None,
    source_coverage: Sequence[SupplementCoverage] | None = None,
    failures: Sequence[str] | None = None,
    platform_job_count: int = 0,
    config: dict[str, Any] | None = None,
) -> str:
    """Render cross-source market evidence without treating indexed leads as supply."""
    config = config or {}
    signals = signals or ()
    evidence_signals = signals if evidence_signals is None else evidence_signals
    failures = failures or ()
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    now = datetime.now(timezone)
    sample_count = int(_value(snapshot, "sample_count", default=0) or 0)
    company_count = int(_value(snapshot, "company_count", default=0) or 0)
    primary_count = int(_value(snapshot, "primary_count", default=0) or 0)
    segments = _market_segments(snapshot)
    primary = segments.get("北京/天津", {})
    primary_count = int(primary.get("sample_count", 0) or 0)
    history_days = int(_value(insight, "history_days", default=0) or 0)
    timing_label = str(_value(insight, "timing_label", default="样本基线待建立"))
    timing_reason = str(
        _value(insight, "timing_reason", default="尚无足够的同口径历史，今日只报横截面，不宣称上升或降温。")
    )
    lines = [
        f"## Agent 求职市场情报｜{now:%Y-%m-%d}",
        "",
        "## 时机判断",
        "",
        f"**{markdown_text(timing_label)}**｜{markdown_text(_compact(timing_reason, 180))}",
        f"同口径历史：{history_days} 个独立日；至少 28 日后才输出“升温 / 降温”结论。",
    ]
    for summary in list(_value(insight, "flow_summary", default=()) or ())[:3]:
        lines.append(f"- **招聘流量：** {markdown_text(_compact(summary, 180))}")
    lines.extend(
        [
        "",
        "## 样本边界与源健康度",
        "",
        f"- **可统计样本：** 本轮仅使用 L1 企业官网完整正文，n={sample_count}，{company_count} 家公司；北京/天津决策样本 {primary_count}",
        (
            f"- **L2 平台正文：** 本轮含本机验活平台岗位 {platform_job_count} 条（BOSS / 猎聘），仅作岗位推送，不进供给统计"
            if platform_job_count
            else "- **L2 平台正文：** 当前没有达到正文验活标准的自动样本；不与官网样本混算"
        ),
        "- **不计入供给统计：** L3 搜索索引线索，L4 公众号 / 小红书 / 行业内容",
        ]
    )
    lines.extend(_official_coverage_summary(official_coverage, snapshot))
    lines.extend(_trend_coverage_summary(trend_coverage, config))
    lines.extend(_content_coverage_summary(trend_coverage))
    if failures:
        lines.append(f"- **本轮异常：** {len(failures)} 项（不用失败源推断市场降温）")
    lines.append("")
    if source_coverage:
        lines.extend(format_source_coverage(list(source_coverage)))

    primary_total = int(primary.get("sample_count", 0) or 0)
    lines.extend(
        [
            f"## 北京 / 天津求职决策样本｜n={primary_total}",
            "",
            f"- **活跃库存发布时间：** 近7日发布 {int(primary.get('new_postings_7d', 0) or 0)}；此前7日发布 {int(primary.get('previous_postings_7d', 0) or 0)}；近28日发布 {int(primary.get('new_postings_28d', 0) or 0)}；可确认发布日期 {int(primary.get('published_date_known_count', 0) or 0)}/{primary_total or 0}",
            f"- **方向：** {_format_distribution(primary.get('directions', {}), primary_total)}",
            f"- **技能：** {_format_distribution(primary.get('skills', {}), primary_total)}",
            f"- **时效：** {_format_distribution(primary.get('freshness', {}), primary_total)}",
        ]
    )
    salary_count = int(primary.get("salary_disclosed_count", 0) or 0)
    salary_label = f"{salary_count}/{primary_total} ({salary_count / primary_total:.0%})" if primary_total else "0/0"
    boundary_count = int(primary.get("work_boundary_signal_count", 0) or 0)
    boundary_label = (
        f"{boundary_count}/{primary_total} ({boundary_count / primary_total:.0%})"
        if primary_total
        else "0/0"
    )
    lines.extend(
        [
            f"- **薪酬披露：** {salary_label}；未披露不等于低于目标包",
            f"- **工时 / 差旅文字信号：** {boundary_label}；仍需面试与反向背调确认真实强度",
            "- 方向与技能为多选编码；北京/天津是行动分母，不由海外岗位替代。",
            "",
        ]
    )

    lines.extend(["## 国内其他城市 / 海外｜仅作参照", ""])
    for label in ("中国其他城市", "海外", "地点未披露"):
        metrics = segments.get(label, {})
        denominator = int(metrics.get("sample_count", 0) or 0)
        if not denominator:
            continue
        purpose = "横向机会参考" if label == "中国其他城市" else "行业前沿参考" if label == "海外" else "不参与地点判断"
        lines.append(
            f"- **{label}：** n={denominator}｜{purpose}｜方向 {_format_distribution(metrics.get('directions', {}), denominator, limit=3)}"
        )
    lines.append("")

    changes = _value(insight, "direction_changes", default=()) or ()
    actions = _value(insight, "actions", default=()) or ()
    if changes or actions:
        lines.extend(["## 判断与下一步", ""])
        for change in list(changes)[:4]:
            if isinstance(change, str):
                text = change
            else:
                name = _value(change, "name", "direction", "label", default="方向信号")
                detail = _value(change, "summary", "detail", "change", default="")
                text = f"{name}：{detail}" if detail else str(name)
            lines.append(f"- {markdown_text(_compact(text, 140))}")
        for action in list(actions)[:4]:
            lines.append(f"- **行动：** {markdown_text(_compact(action, 140))}")
        lines.append("")

    platform_signals = [signal for signal in signals if signal.kind == "platform"]
    content_signals = [signal for signal in signals if signal.kind != "platform"]
    evidence_content_signals = list(
        {
            signal.identity: signal
            for signal in evidence_signals
            if signal.kind != "platform"
        }.values()
    )
    lines.extend(["## 社招高阶线索｜招聘平台 / 公共就业 / 人才网（含猎聘）", ""])
    lines.append("以下是 L3 公开索引线索，不等同于企业官网在招，不计入上方市场样本。")
    if platform_signals:
        for signal in platform_signals[:5]:
            lines.extend(
                [
                    f"- **{markdown_text(signal.title)}**｜{markdown_text(signal.source)}｜{markdown_text(_signal_dates(signal, timezone))}",
                    f"  {markdown_text(signal_excerpt(signal, 72))}",
                ]
            )
            if is_stable_public_signal_url(signal.url):
                lines.append(f"  {markdown_link('打开岗位线索并确认仍在招 →', signal.url)}")
            else:
                lines.append(
                    f"  无稳定直达链接｜请在{markdown_text(signal.source)}搜索完整标题：「{markdown_text(signal.title)}」"
                )
    else:
        lines.append("本轮无通过详情页规则的平台索引线索。")
    lines.append("")

    lines.extend(["## 行业报告 / 公众号 / 小红书｜趋势参考（L4 方向证据）", ""])
    lines.append(
        "- **交叉佐证：** "
        + markdown_text(
            _content_corroboration(
                evidence_content_signals,
                now,
                int(config.get("trend_signal_max_age_days", 45)),
            )
        )
    )
    if content_signals:
        for signal in content_signals[:6]:
            lines.extend(
                [
                    f"- **{markdown_text(signal.title)}**｜{markdown_text(signal.source)}｜{markdown_text(_signal_dates(signal, timezone))}",
                    f"  {markdown_text(signal_excerpt(signal, 72))}",
                ]
            )
            if is_stable_public_signal_url(signal.url):
                lines.append(f"  {markdown_link('查看稳定公开原文 →', signal.url)}")
            else:
                source_hint = "小红书" if "小红书" in signal.source else "微信公众号" if any(value in signal.source for value in ("微信", "公众号")) else signal.source
                search_url = signal_title_search_url(signal)
                fallback = (
                    markdown_link(f"在{source_hint}按标题检索 →", search_url)
                    if search_url
                    else f"请在{markdown_text(source_hint)}搜索完整标题"
                )
                lines.append(f"  原文无稳定直达链接｜{fallback}")
    else:
        if evidence_content_signals:
            lines.append("本轮没有新的 L4 展示项；交叉佐证仍使用本轮检索到的有效证据存量。")
        else:
            lines.append("本轮没有可公开核验的 L4 内容证据。")
    lines.extend(
        [
            "",
            "市场统计只使用正文验活岗位；平台索引和社交内容只用于找线索与校验方向。",
        ]
    )
    return truncate_utf8("\n".join(lines), MAX_MESSAGE_BYTES)


def format_report(
    items: list[Assessment],
    discovered_count: int,
    failures: list[str],
    *,
    trend_items: list[Assessment] | None = None,
    signals: list[TrendSignal] | None = None,
    source_coverage: list[SupplementCoverage] | tuple[SupplementCoverage, ...] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Backward-compatible combined report; new callers should send two reports."""
    trend_items = trend_items or []
    action = format_action_report(
        items,
        discovered_count,
        failures,
        trend_items=trend_items,
        config=config,
    )
    assessed = [*items, *trend_items]
    themes = Counter(
        responsibility for item in assessed for responsibility in item.responsibilities
    )
    companies = {item.job.company or item.job.source for item in assessed}
    fallback_snapshot = {
        "sample_count": len(assessed),
        "company_count": len(companies),
        "primary_count": len(items),
        "directions": dict(themes),
        "skills": {},
        "locations": dict(Counter(item.job.location for item in assessed)),
        "freshness": {},
        "salary_disclosed_count": sum(item.salary.label != "未披露" for item in assessed),
    }
    market = format_market_report(
        fallback_snapshot,
        signals=signals,
        source_coverage=source_coverage,
        failures=failures,
        config=config,
    )
    return truncate_utf8(f"{action}\n\n---\n\n{market}", MAX_MESSAGE_BYTES)


def validate_dingtalk_webhook(webhook: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(webhook)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    valid = (
        parsed.scheme == "https"
        and parsed.hostname == "oapi.dingtalk.com"
        and parsed.path == "/robot/send"
        and bool(query.get("access_token"))
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )
    if not valid:
        raise ValueError("DINGTALK_WEBHOOK 不是有效的钉钉自定义机器人地址")
    return parsed


def signed_webhook_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    parsed = validate_dingtalk_webhook(webhook)
    timestamp_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    string_to_sign = f"{timestamp_ms}\n{secret}".encode("utf-8")
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    ).decode()
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((("timestamp", str(timestamp_ms)), ("sign", signature)))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def send_dingtalk(
    markdown: str,
    webhook: str,
    secret: str,
    *,
    title: str = "Sunny 每日岗位雷达",
) -> None:
    validate_dingtalk_webhook(webhook)
    safe_title = _compact(title, 64) or "Sunny 每日岗位雷达"
    payload = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"title": safe_title, "text": truncate_utf8(markdown, MAX_MESSAGE_BYTES)},
            "at": {"isAtAll": False},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    opener = urllib.request.build_opener(NoRedirectHandler())
    for attempt in range(3):
        request = urllib.request.Request(
            signed_webhook_url(webhook, secret),
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("errcode") != 0:
                raise RuntimeError(f"钉钉拒绝消息：{result.get('errmsg', 'unknown error')}")
            return
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == 2:
                raise RuntimeError(f"钉钉 HTTP 错误：{exc.code}") from exc
            retry_after = exc.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 8))
        except urllib.error.URLError as exc:
            if attempt == 2:
                raise RuntimeError("钉钉网络请求失败") from exc
            time.sleep(2**attempt)
