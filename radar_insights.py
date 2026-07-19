"""Evidence-bounded market snapshots and cautious longitudinal insights."""

from __future__ import annotations

import json
import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from radar_market import job_freshness, location_bucket, parse_timestamp
from radar_matching import (
    contains_any,
    has_substantive_job_description,
    has_target_title,
    looks_like_candidate_job,
    looks_like_product_job,
)
from radar_types import Assessment, SourceCoverage, normalize_url


MIN_TREND_DAYS = 28
HISTORY_RETENTION_DAYS = 120
MIN_FLOW_SAMPLE = 8
METHODOLOGY_VERSION = 2


DIRECTION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Evals / Benchmark / 可靠性",
        (
            "eval", "evaluation", "benchmark", "评测", "模型评估", "badcase",
            "failure attribution", "失败归因", "reliability", "可靠性",
        ),
    ),
    (
        "多模态与端侧",
        ("multimodal", "multi-modal", "多模态", "on-device", "edge ai", "端侧", "设备端"),
    ),
    (
        "Agent 平台 / 工具链",
        ("agent platform", "agent infrastructure", "mcp", "tool calling", "tool use", "sdk", "工具调用", "开发者平台"),
    ),
    (
        "AI 安全与治理",
        ("ai safety", "trust & safety", "responsible ai", "security", "safeguard", "安全", "风险治理", "合规"),
    ),
    (
        "部署与行业落地",
        ("deployment", "deployed", "implementation", "production", "部署", "生产化", "行业落地", "业务落地"),
    ),
    (
        "Agent 应用产品",
        ("agent", "agentic", "智能体", "任务闭环", "产品闭环"),
    ),
)


SKILL_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Python / API / 原型",
        ("python", "typescript", " api ", "api、", "api/", "prototype", "prototyping", "原型"),
    ),
    (
        "trace / 失败归因",
        ("trace", "tracing", "observability", "failure attribution", "失败归因", "可观测", "badcase"),
    ),
    (
        "tool / context / memory",
        ("tool calling", "tool use", "mcp", "context", "memory", "rag", "工具调用", "上下文", "记忆"),
    ),
    (
        "成本 / 延迟 / ROI",
        ("cost", "latency", " roi", "business outcome", "成本", "延迟", "投入产出", "业务结果"),
    ),
    (
        "安全 / 可靠性",
        ("safety", "security", "reliability", "robustness", "安全", "可靠性", "稳定性", "可控性"),
    ),
)


@dataclass(frozen=True)
class MarketSnapshot:
    captured_at: str
    snapshot_date: str
    sample_count: int
    company_count: int
    primary_count: int
    directions: dict[str, int]
    skills: dict[str, int]
    locations: dict[str, int]
    freshness: dict[str, int]
    salary_disclosed_count: int
    work_boundary_signal_count: int
    official_sources_ok: int
    official_sources_planned: int
    decision_sources_ok: int
    decision_sources_planned: int
    source_keys_ok: tuple[str, ...]
    source_samples: dict[str, dict[str, Any]]
    sample_identities: tuple[str, ...]
    methodology_id: str
    segments: dict[str, dict[str, Any]]
    sample_identity_sources: dict[str, str]
    sample_identity_segments: dict[str, str]
    sample_identity_directions: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class MarketInsight:
    history_days: int
    timing_label: str
    timing_reason: str
    direction_changes: tuple[str, ...]
    actions: tuple[str, ...]
    flow_summary: tuple[str, ...] = ()


def _contains(text: str, terms: Iterable[str]) -> bool:
    folded = f" {text.casefold()} "
    return any(term.casefold() in folded for term in terms)


def _counter_for(texts: Iterable[str], taxonomy: Sequence[tuple[str, tuple[str, ...]]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for text in texts:
        for label, terms in taxonomy:
            if _contains(text, terms):
                counts[label] += 1
    return dict(counts.most_common())


def _methodology_id(config: Mapping[str, Any]) -> str:
    """Fingerprint every input that can materially change the market denominator."""
    sources: list[dict[str, Any]] = []
    for raw in config.get("official_sources", ()):
        if not isinstance(raw, Mapping):
            continue
        sources.append({
            str(key): value
            for key, value in raw.items()
            if not any(marker in str(key).casefold() for marker in ("secret", "token", "cookie", "password", "auth"))
        })
    payload = {
        "version": METHODOLOGY_VERSION,
        "directions": DIRECTION_TERMS,
        "skills": SKILL_TERMS,
        "primary_locations": sorted(str(value) for value in config.get("primary_locations", ())),
        "max_job_age_days": int(config.get("max_job_age_days", 180)),
        "fit_threshold": int(config.get("fit_threshold", 0)),
        "ready_threshold": int(config.get("ready_threshold", 0)),
        "current_fixed_cash_wan": config.get("current_fixed_cash_wan"),
        "target_total_comp_wan": config.get("target_total_comp_wan"),
        "usd_cny": config.get("usd_cny"),
        "max_detail_fetches": config.get("max_detail_fetches"),
        "max_results_per_query": config.get("max_results_per_query"),
        "official_discovery_budget_seconds": config.get("official_discovery_budget_seconds"),
        "official_source_budget_seconds": config.get("official_source_budget_seconds"),
        "job_search_budget_seconds": config.get("job_search_budget_seconds"),
        "queries": config.get("queries", ()),
        "trusted_job_hosts": config.get("trusted_job_hosts", ()),
        "employer_career_hosts": config.get(
            "employer_career_hosts", config.get("official_career_hosts", ())
        ),
        "sources": sorted(sources, key=lambda value: str(value.get("key", ""))),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"v{METHODOLOGY_VERSION}-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _target_market_job(item: Assessment) -> bool:
    """Keep only personally relevant Agent/AI product ownership roles in the denominator."""
    body_target = looks_like_product_job(item.job) and contains_any(
        item.job.summary,
        (
            "agent", "agentic", "智能体", "eval", "evaluation", "benchmark", "评测",
            "reliability", "可靠性", "multimodal", "multi-modal", "多模态",
            "large language model", "llm", "generative ai", "大模型", "生成式人工智能",
        ),
    )
    return bool(
        item.eligible
        and (has_target_title(item.job.title) or body_target)
        and looks_like_candidate_job(item.job)
    )


def _substantive(
    item: Assessment,
    config: Mapping[str, Any],
    now: datetime,
) -> bool:
    job = item.job
    # A market-supply sample is stricter than a search result: official, live,
    # target-shaped, and detailed enough to support skill/responsibility coding.
    return bool(
        job.official
        and job.active
        and has_substantive_job_description(job)
        and _target_market_job(item)
        and job_freshness(job, dict(config), now).trend_eligible
    )


def _dedupe_assessments(
    values: Iterable[Assessment],
    config: Mapping[str, Any],
    now: datetime,
) -> list[tuple[str, Assessment]]:
    selected: list[Assessment] = []
    canonical_records: list[Assessment] = []
    id_to_index: dict[str, int] = {}
    url_to_indices: dict[str, set[int]] = {}
    fuzzy_to_indices: dict[str, set[int]] = {}
    for item in values:
        if not _substantive(item, config, now):
            continue
        job = item.job
        stable_url = normalize_url(job.url)
        id_alias = ""
        if job.job_id:
            id_alias = (
                f"id:{(job.source_key or job.source).casefold().strip()}:"
                f"{job.job_id.casefold().strip()}"
            )
        fuzzy_alias = ""
        if not id_alias and not stable_url:
            company = (job.company or job.source_key or job.source).casefold().strip()
            title = re.sub(r"\s+", " ", job.title.casefold()).strip()
            location = re.sub(r"\s+", " ", job.location.casefold()).strip()
            fuzzy_alias = f"fuzzy:{company}\n{title}\n{location}"

        index: int | None = id_to_index.get(id_alias) if id_alias else None
        if index is None and stable_url:
            url_matches = url_to_indices.get(stable_url, set())
            if job.job_id:
                compatible = {
                    candidate
                    for candidate in url_matches
                    if not canonical_records[candidate].job.job_id
                    or canonical_records[candidate].job.job_id.casefold() == job.job_id.casefold()
                }
                # Distinct requisition IDs are never collapsed merely because an
                # ATS encodes them in a URL fragment that normalization removes.
                if len(url_matches) == 1 and len(compatible) == 1:
                    index = next(iter(compatible))
            elif len(url_matches) == 1:
                # A URL can bridge an ID-less fallback record only while it maps
                # unambiguously to one official requisition.
                index = next(iter(url_matches))
        if index is None and fuzzy_alias:
            fuzzy_matches = fuzzy_to_indices.get(fuzzy_alias, set())
            if len(fuzzy_matches) == 1:
                index = next(iter(fuzzy_matches))

        if index is not None:
            if job.job_id and not canonical_records[index].job.job_id:
                canonical_records[index] = item
            if len(" ".join(job.summary.split())) > len(" ".join(selected[index].job.summary.split())):
                selected[index] = item
        else:
            index = len(selected)
            selected.append(item)
            canonical_records.append(item)
        if id_alias:
            id_to_index[id_alias] = index
        if stable_url:
            url_to_indices.setdefault(stable_url, set()).add(index)
        if fuzzy_alias:
            fuzzy_to_indices.setdefault(fuzzy_alias, set()).add(index)
    merged: list[tuple[str, Assessment]] = []
    for canonical, representative in zip(canonical_records, selected):
        canonical_job = replace(
            canonical.job,
            summary=representative.job.summary,
        )
        merged.append((canonical.job.identity, replace(representative, job=canonical_job)))
    return merged


def _freshness_bucket(item: Assessment, now: datetime) -> str:
    posted = parse_timestamp(item.job.published_at)
    if not posted or item.job.date_basis not in {"published", "created"}:
        return "日期未披露"
    age = max(0, (now - posted).days)
    if age <= 30:
        return "0–30天"
    if age <= 90:
        return "31–90天"
    if age <= 180:
        return "91–180天"
    return "180天以上"


def _published_age_days(item: Assessment, now: datetime) -> float | None:
    if item.job.date_basis not in {"published", "created"}:
        return None
    posted = parse_timestamp(item.job.published_at)
    if not posted or posted > now + timedelta(hours=48):
        return None
    return max(0.0, (now - posted).total_seconds() / 86_400)


def _segment_metrics(items: Sequence[Assessment], now: datetime) -> dict[str, Any]:
    texts = [item.job.text for item in items]
    recent = [item for item in items if (age := _published_age_days(item, now)) is not None and age < 7]
    previous = [
        item
        for item in items
        if (age := _published_age_days(item, now)) is not None and 7 <= age < 14
    ]
    last_28 = [item for item in items if (age := _published_age_days(item, now)) is not None and age < 28]
    return {
        "sample_count": len(items),
        "company_count": len(
            {
                (item.job.company or item.job.source_key or item.job.source).casefold().strip()
                for item in items
            }
        ),
        "directions": _counter_for(texts, DIRECTION_TERMS),
        "skills": _counter_for(texts, SKILL_TERMS),
        "freshness": dict(Counter(_freshness_bucket(item, now) for item in items).most_common()),
        "salary_disclosed_count": sum(item.salary.label != "未披露" for item in items),
        "work_boundary_signal_count": sum(
            _contains(
                item.job.text,
                (
                    "on-call", "on call", "weekend", "overtime", "fast-paced",
                    "客户现场", "跨时区", "出差", "值班", "周末", "加班", "高强度", "快节奏",
                ),
            )
            for item in items
        ),
        "published_date_known_count": sum(_published_age_days(item, now) is not None for item in items),
        "new_postings_7d": len(recent),
        "previous_postings_7d": len(previous),
        "new_postings_28d": len(last_28),
        "new_directions_7d": _counter_for((item.job.text for item in recent), DIRECTION_TERMS),
        "previous_directions_7d": _counter_for((item.job.text for item in previous), DIRECTION_TERMS),
    }


def build_market_snapshot(
    assessments: Iterable[Assessment],
    coverage: Sequence[SourceCoverage],
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> MarketSnapshot:
    """Build one transparent cross-source sample from verified official JDs only."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sample_rows = _dedupe_assessments(assessments, config, current)
    sample = [item for _, item in sample_rows]
    canonical_identity = {id(item): identity for identity, item in sample_rows}
    texts = [item.job.text for item in sample]
    companies = {
        (item.job.company or item.job.source_key or item.job.source).casefold().strip()
        for item in sample
    }
    segment_items: dict[str, list[Assessment]] = {
        "北京/天津": [],
        "中国其他城市": [],
        "海外": [],
        "地点未披露": [],
    }
    for item in sample:
        bucket = location_bucket(item.job, dict(config))
        label = {
            "primary": "北京/天津",
            "china_other": "中国其他城市",
            "global": "海外",
            "unknown": "地点未披露",
        }.get(bucket, bucket)
        segment_items.setdefault(label, []).append(item)

    segments = {
        label: _segment_metrics(items, current)
        for label, items in segment_items.items()
    }
    location_counts = Counter(
        {label: int(metrics["sample_count"]) for label, metrics in segments.items()}
    )
    freshness_counts = Counter(_freshness_bucket(item, current) for item in sample)

    planned = len(coverage)
    healthy = tuple(row.source_key for row in coverage if row.status == "ok")
    decision_coverage = [row for row in coverage if row.scope == "china"]
    decision_healthy = tuple(row.source_key for row in decision_coverage if row.status == "ok")
    source_samples: dict[str, dict[str, Any]] = {}
    primary_sample = segment_items["北京/天津"]
    for source_key in sorted({item.job.source_key or item.job.source for item in primary_sample}):
        source_items = [
            item for item in primary_sample if (item.job.source_key or item.job.source) == source_key
        ]
        source_samples[source_key] = _segment_metrics(source_items, current)
    return MarketSnapshot(
        captured_at=current.isoformat(timespec="seconds"),
        snapshot_date=current.date().isoformat(),
        sample_count=len(sample),
        company_count=len(companies),
        primary_count=location_counts.get("北京/天津", 0),
        directions=_counter_for(texts, DIRECTION_TERMS),
        skills=_counter_for(texts, SKILL_TERMS),
        locations=dict(location_counts.most_common()),
        freshness=dict(freshness_counts.most_common()),
        salary_disclosed_count=sum(item.salary.label != "未披露" for item in sample),
        work_boundary_signal_count=sum(
            _contains(
                item.job.text,
                (
                    "travel", "on-call", "on call", "weekend", "overtime", "fast-paced",
                    "出差", "值班", "周末", "加班", "高强度", "快节奏",
                ),
            )
            for item in sample
        ),
        official_sources_ok=len(healthy),
        official_sources_planned=planned,
        decision_sources_ok=len(decision_healthy),
        decision_sources_planned=len(decision_coverage),
        source_keys_ok=healthy,
        source_samples=source_samples,
        sample_identities=tuple(sorted(canonical_identity[id(item)] for item in sample)),
        methodology_id=_methodology_id(config),
        segments=segments,
        sample_identity_sources={
            canonical_identity[id(item)]: item.job.source_key or item.job.source for item in sample
        },
        sample_identity_segments={
            canonical_identity[id(item)]: {
                "primary": "北京/天津",
                "china_other": "中国其他城市",
                "global": "海外",
                "unknown": "地点未披露",
            }.get(location_bucket(item.job, dict(config)), "地点未披露")
            for item in sample
        },
        sample_identity_directions={
            canonical_identity[id(item)]: tuple(
                label for label, terms in DIRECTION_TERMS if _contains(item.job.text, terms)
            )
            for item in sample
        },
    )


def _snapshot_dict(snapshot: MarketSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    return asdict(snapshot) if isinstance(snapshot, MarketSnapshot) else dict(snapshot)


def append_market_snapshot(
    history: Sequence[Mapping[str, Any]],
    snapshot: MarketSnapshot | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Replace the same day and discard history produced by an incompatible method."""
    row = _snapshot_dict(snapshot)
    date = str(row.get("snapshot_date") or str(row.get("captured_at", ""))[:10])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("market snapshot is missing a valid snapshot_date")
    row["snapshot_date"] = date
    methodology = str(row.get("methodology_id", ""))
    merged = [
        dict(value)
        for value in history
        if str(value.get("snapshot_date")) != date
        and str(value.get("methodology_id", "")) == methodology
    ]
    merged.append(row)
    merged.sort(key=lambda value: str(value.get("snapshot_date", "")))
    newest = parse_timestamp(f"{merged[-1]['snapshot_date']}T00:00:00Z")
    if newest:
        cutoff = newest - timedelta(days=HISTORY_RETENTION_DAYS - 1)
        merged = [
            value
            for value in merged
            if (parse_timestamp(f"{value.get('snapshot_date', '')}T00:00:00Z") or newest) >= cutoff
        ]
    return merged


def load_market_history(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = payload.get("snapshots", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    valid: list[dict[str, Any]] = []
    for value in rows:
        if isinstance(value, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("snapshot_date", ""))):
            valid.append(dict(value))
    return valid


def save_market_history(path: Path, history: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshots": [dict(value) for value in history],
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _distinct_history(
    current: MarketSnapshot,
    history: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    combined = append_market_snapshot(history, current)
    return combined


def _cross_section_actions(current: MarketSnapshot) -> tuple[str, ...]:
    primary = current.segments.get("北京/天津", {})
    total = int(primary.get("sample_count", 0) or 0)
    if total <= 0:
        return ("北京/天津当日可编码目标样本为 0；先核对来源并保持机会池，不用空结果判断市场冷热",)
    actions: list[str] = []
    directions = primary.get("directions", {})
    skills = primary.get("skills", {})
    evals = int(directions.get("Evals / Benchmark / 可靠性", 0) or 0) if isinstance(directions, Mapping) else 0
    health = (
        current.decision_sources_ok / current.decision_sources_planned
        if current.decision_sources_planned
        else 0.0
    )
    if total >= 8 and evals >= 2 and health >= 0.5:
        actions.append(
            f"北京/天津目标样本已有可定向验证供给：Evals/Benchmark/可靠性 {evals}/{total}；"
            "建议在职小步试投，不必等统一“金三银四”，也不支持裸辞"
        )
    else:
        actions.append("北京/天津目标岗位或 Evals 交集样本偏少，保持精选机会池，不扩大海投")

    builder = int(skills.get("Python / API / 原型", 0) or 0) if isinstance(skills, Mapping) else 0
    trace = int(skills.get("trace / 失败归因", 0) or 0) if isinstance(skills, Mapping) else 0
    actions.append(
        f"北京/天津 JD 中，Python/API/原型为 {builder}/{total}，"
        f"trace/失败归因为 {trace}/{total}；优先做一个可演示的 Agent+Evals 闭环"
    )
    salary = int(primary.get("salary_disclosed_count", 0) or 0)
    actions.append(
        f"北京/天津仅 {salary}/{total} 个 JD 披露薪酬；"
        "市场公开数据不足以判断是否达标，应在猎头/招聘者首轮前置核验现金与股票可兑现口径"
    )
    boundary = int(primary.get("work_boundary_signal_count", 0) or 0)
    actions.append(
        f"北京/天津只有 {boundary}/{total} 个 JD 出现工时/差旅文字；"
        "双休、21 点后工作频率和差旅不能从缺失值推断，必须反向背调"
    )
    return tuple(actions)


def _stock_flow_summary(
    current: MarketSnapshot,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    primary = current.segments.get("北京/天津", {})
    recent = int(primary.get("new_postings_7d", 0) or 0)
    previous = int(primary.get("previous_postings_7d", 0) or 0)
    month = int(primary.get("new_postings_28d", 0) or 0)
    known = int(primary.get("published_date_known_count", 0) or 0)
    stock = int(primary.get("sample_count", 0) or 0)
    summary = [
        f"北京/天津活跃库存 {stock}；其中近7日发布 {recent}，此前7日发布 {previous}，近28日发布 {month}；"
        f"发布日期可确认 {known}/{stock or 0}"
    ]

    prior_rows = [
        row for row in rows
        if str(row.get("snapshot_date", "")) < current.snapshot_date
    ]
    if not prior_rows:
        summary.append("尚无上个可比扫描，暂不报告关闭/净新增")
        return tuple(summary)
    prior = prior_rows[-1]
    current_sources = set(current.source_keys_ok)
    prior_sources = {
        str(value) for value in prior.get("source_keys_ok", ())
    }
    common_sources = current_sources & prior_sources
    current_source_map = current.sample_identity_sources
    current_segment_map = current.sample_identity_segments
    prior_source_map = prior.get("sample_identity_sources", {})
    prior_segment_map = prior.get("sample_identity_segments", {})
    if not common_sources or not isinstance(prior_source_map, Mapping) or not isinstance(prior_segment_map, Mapping):
        summary.append("上个扫描缺少同口径健康源，暂不报告关闭/净新增")
        return tuple(summary)
    current_ids = {
        identity for identity, source in current_source_map.items()
        if source in common_sources and current_segment_map.get(identity) == "北京/天津"
    }
    prior_ids = {
        str(identity) for identity, source in prior_source_map.items()
        if str(source) in common_sources and prior_segment_map.get(identity) == "北京/天津"
    }
    summary.append(
        f"较上个可比扫描：活跃库存新出现 {len(current_ids - prior_ids)}，本轮未再发现 {len(prior_ids - current_ids)}；"
        "后者是疑似下线，仍可能受官网改版影响"
    )
    return tuple(summary)


def _stable_healthy_sources(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    window = rows[-MIN_TREND_DAYS:]
    counts: Counter[str] = Counter()
    for row in window:
        raw = row.get("source_keys_ok", ())
        if isinstance(raw, (list, tuple, set)):
            counts.update(str(value) for value in raw)
    minimum = max(1, int(len(window) * 0.8 + 0.999))
    return {source for source, count in counts.items() if count >= minimum}


def _observed_primary_inflow(
    rows: Sequence[Mapping[str, Any]],
    source_keys: set[str],
    current_date: str,
) -> tuple[int, int, Counter[str], Counter[str]]:
    current_day = parse_timestamp(f"{current_date}T00:00:00Z")
    if not current_day:
        return 0, 0, Counter(), Counter()
    first_seen: dict[str, tuple[str, tuple[str, ...]]] = {}
    for row in sorted(rows, key=lambda value: str(value.get("snapshot_date", ""))):
        date = str(row.get("snapshot_date", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        healthy = {str(value) for value in row.get("source_keys_ok", ())}
        sources = row.get("sample_identity_sources", {})
        segments = row.get("sample_identity_segments", {})
        directions = row.get("sample_identity_directions", {})
        if not all(isinstance(value, Mapping) for value in (sources, segments, directions)):
            continue
        for identity, raw_source in sources.items():
            source = str(raw_source)
            if source not in source_keys or source not in healthy:
                continue
            if segments.get(identity) != "北京/天津" or str(identity) in first_seen:
                continue
            raw_directions = directions.get(identity, ())
            tags = tuple(str(value) for value in raw_directions) if isinstance(raw_directions, (list, tuple)) else ()
            first_seen[str(identity)] = (date, tags)

    recent = previous = 0
    recent_directions: Counter[str] = Counter()
    previous_directions: Counter[str] = Counter()
    for date, tags in first_seen.values():
        observed = parse_timestamp(f"{date}T00:00:00Z")
        if not observed:
            continue
        age = (current_day - observed).days
        if 0 <= age < 7:
            recent += 1
            recent_directions.update(tags)
        elif 7 <= age < 14:
            previous += 1
            previous_directions.update(tags)
    return recent, previous, recent_directions, previous_directions


def analyze_market(
    current: MarketSnapshot,
    history: Sequence[Mapping[str, Any]],
) -> MarketInsight:
    """Judge Beijing/Tianjin posting flow only after a comparable 28-day baseline."""
    rows = _distinct_history(current, history)
    history_days = len({str(row.get("snapshot_date", "")) for row in rows})
    flow_summary = _stock_flow_summary(current, rows)
    if history_days < MIN_TREND_DAYS:
        return MarketInsight(
            history_days=history_days,
            timing_label="基线积累中",
            timing_reason=(
                f"已积累 {history_days}/{MIN_TREND_DAYS} 个独立日样本；"
                "当前只报告北京/天津活跃库存与可确认发布时间，不把搜索波动冒充升温/降温。"
            ),
            direction_changes=(),
            actions=_cross_section_actions(current),
            flow_summary=flow_summary,
        )

    stable_sources = _stable_healthy_sources(rows) & set(current.source_keys_ok)
    if not stable_sources:
        return MarketInsight(
            history_days=history_days,
            timing_label="样本口径变化，暂缓判断",
            timing_reason=(
                "虽已满足 28 个独立日，但没有在至少 80% 基线日保持健康、且今天仍正常的共同官网来源；"
                "不将来源故障解读为市场趋势。"
            ),
            direction_changes=(),
            actions=(
                "先修复官网来源连续性，不把口径变化解读为市场冷热",
                *_cross_section_actions(current),
            ),
            flow_summary=flow_summary,
        )

    recent, previous, recent_directions, previous_directions = _observed_primary_inflow(
        rows, stable_sources, current.snapshot_date
    )
    flow_summary = (
        *flow_summary,
        f"同口径健康官网的雷达首次发现：近7日 {recent}，此前7日 {previous}（不是招聘平台全量投递数）",
    )
    changes: list[str] = []
    if recent >= MIN_FLOW_SAMPLE and previous >= MIN_FLOW_SAMPLE:
        for direction in set(recent_directions) | set(previous_directions):
            delta = recent_directions[direction] / recent - previous_directions[direction] / previous
            if abs(delta) >= 0.2:
                changes.append(
                    f"北京/天津新增岗位中，{direction}占比{'上升' if delta > 0 else '下降'} {abs(delta):.0%}"
                )

    source_health = (
        current.decision_sources_ok / current.decision_sources_planned
        if current.decision_sources_planned
        else 0.0
    )
    primary_count = int(current.segments.get("北京/天津", {}).get("sample_count", 0) or 0)
    if source_health < 0.5 or primary_count < 3:
        changes = []
        label = "样本不足，暂缓市场结论"
        reason = (
            f"中国决策源健康度 {current.decision_sources_ok}/{current.decision_sources_planned}，"
            f"北京/天津目标岗位活跃库存 n={primary_count}；先修复来源再判断时机。"
        )
    elif recent < MIN_FLOW_SAMPLE or previous < MIN_FLOW_SAMPLE:
        changes = []
        label = "新增流量样本不足，维持精选"
        reason = (
            f"稳定健康源中的北京/天津雷达首次发现近7日 {recent}、此前7日 {previous}；"
            f"任一窗口不足 {MIN_FLOW_SAMPLE} 条，不宣称升温或降温。"
        )
    elif recent >= previous * 1.25 and recent - previous >= 2:
        label = "北京/天津雷达新增供给升温，可小步试投"
        reason = (
            f"稳定健康源中雷达首次发现近7日 {recent}，此前7日 {previous}；"
            "以在职小步试投验证薪酬和强度，不据此裸辞。"
        )
    elif recent <= previous * 0.75 and previous - recent >= 2:
        label = "北京/天津新增放缓，维持精选"
        reason = f"稳定健康源中雷达首次发现近7日 {recent}，此前7日 {previous}；收窄到高匹配机会。"
    else:
        label = "北京/天津新增平稳，定向建立机会池"
        reason = f"稳定健康源中雷达首次发现近7日 {recent}，此前7日 {previous}；未达到 25% 且至少 2 条的变化阈值。"

    return MarketInsight(
        history_days=history_days,
        timing_label=label,
        timing_reason=reason,
        direction_changes=tuple(sorted(changes)),
        actions=_cross_section_actions(current),
        flow_summary=flow_summary,
    )
