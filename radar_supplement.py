"""Validated boundary for locally collected Agent Reach evidence."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from radar_market import parse_timestamp
from radar_types import TrendSignal, is_public_http_url, normalize_url


ALLOWED_STATUSES = {
    "ok", "no_results", "auth_required", "unsupported", "error", "skipped",
}
CHANNEL_HOSTS = {
    "boss": {"www.zhipin.com", "m.zhipin.com"},
    "linkedin": {"www.linkedin.com", "cn.linkedin.com", "sg.linkedin.com"},
    "maimai": {"maimai.cn", "www.maimai.cn"},
    "wechat": {"mp.weixin.qq.com"},
    # Tokenless Xiaohongshu permalinks currently fall through to a security/404
    # page, so Xiaohongshu evidence is deliberately non-clickable.
    "xiaohongshu": set(),
}
CHANNEL_LABELS = {
    "boss": "BOSS直聘",
    "linkedin": "LinkedIn",
    "maimai": "脉脉（Agent Reach）",
    "wechat": "微信公众号（Agent Reach 检索摘要）",
    "xiaohongshu": "小红书（Agent Reach 实读）",
}
SENSITIVE_KEY_PARTS = {
    "access_token", "authorization", "cookie", "openid", "pass_ticket",
    "raw", "raw_html", "raw_response", "request_headers", "response_headers",
    "signature", "sogou_token", "token", "user_id", "xsec_token",
}
SENSITIVE_QUERY_KEYS = {
    "access_token", "auth", "authorization", "cookie", "key", "openid",
    "pass_ticket", "signature", "sogou_token", "token", "xsec_token",
}
TEMPLATE_PHRASES = (
    "boss直聘为您提供", "找工作就上boss直聘", "在线开聊约面试",
    "点击查看详情", "登录后查看更多", "登录后查看完整",
)
ALLOWED_TOP_LEVEL_KEYS = {"schema_version", "generated_at", "coverage", "items"}
ALLOWED_COVERAGE_KEYS = {
    "channel", "status", "summary", "queries", "raw_count", "relevant_count",
    "detail_reads",
}
ALLOWED_ITEM_KEYS = {
    "channel", "kind", "source", "title", "summary", "published_at",
    "observed_at", "evidence", "url",
}
SOURCE_SUFFIX_RE = re.compile(r"^[^|｜\n]{1,30}$")
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:xsec[ _-]*token|sogou[ _-]*token|access[ _-]*token|authorization|"
    r"cookie|pass[ _-]*ticket|user[ _-]*id|raw[ _-]*(?:response|html))"
)
EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")


class SupplementValidationError(ValueError):
    """Raised when a local supplement violates the sanitized interchange contract."""


@dataclass(frozen=True)
class SupplementCoverage:
    channel: str
    status: str
    summary: str = ""
    queries: int = 0
    raw_count: int = 0
    relevant_count: int = 0
    detail_reads: int = 0


@dataclass(frozen=True)
class SupplementBundle:
    schema_version: int
    generated_at: str
    coverage: tuple[SupplementCoverage, ...]
    signals: tuple[TrendSignal, ...]


def _normalized_key(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _reject_sensitive_keys(value: Any, path: str = "root") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in SENSITIVE_KEY_PARTS or normalized.startswith(("xsec_", "cookie_")):
                raise SupplementValidationError(f"补充包包含敏感字段: {path}.{key}")
            _reject_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if SENSITIVE_VALUE_RE.search(value) or EMAIL_RE.search(value) or PHONE_RE.search(value):
            raise SupplementValidationError(f"补充包文本包含敏感内容: {path}")


def _require_known_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise SupplementValidationError(f"{path} 包含未知字段: {sorted(unknown)[0]}")


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SupplementValidationError(f"{field} 必须是非负整数")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    parsed = parse_timestamp(value)
    if not parsed:
        raise SupplementValidationError(f"{field} 必须是有效时间")
    return parsed.astimezone(timezone.utc)


def _validate_url(channel: str, value: Any, kind: str) -> str:
    url = str(value or "").strip()
    if channel == "boss":
        raise SupplementValidationError("BOSS 补充证据尚未开放；需实现可验证的正文实读证明")
    if not url:
        if kind == "platform":
            raise SupplementValidationError("平台岗位证据必须有已验活的职位详情 URL")
        return ""
    if channel == "xiaohongshu":
        raise SupplementValidationError("小红书临时登录链接不得进入补充包")
    if not is_public_http_url(url):
        raise SupplementValidationError("补充包 URL 不是公开 HTTP(S) 地址")
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise SupplementValidationError("补充包 URL 不得含用户信息或片段")
    host = (parsed.hostname or "").casefold()
    if host not in CHANNEL_HOSTS[channel]:
        raise SupplementValidationError(f"{channel} 与 URL 域名不匹配")
    query_keys = {_normalized_key(key) for key, _ in urllib.parse.parse_qsl(parsed.query)}
    if any(key in SENSITIVE_QUERY_KEYS or key.startswith("xsec_") for key in query_keys):
        raise SupplementValidationError("补充包 URL 含敏感访问参数")
    path = parsed.path
    if channel == "linkedin" and not re.fullmatch(r"/jobs/view/(?:[^/]*-)?\d+/?", path):
        raise SupplementValidationError("LinkedIn 证据必须是职位详情直达页")
    if channel == "wechat":
        if path != "/s" and not path.startswith("/s/"):
            raise SupplementValidationError("公众号 URL 必须是 mp.weixin.qq.com 原文")
        allowed = {"__biz", "mid", "idx", "sn"}
        if any(key not in allowed for key, _ in urllib.parse.parse_qsl(parsed.query)):
            raise SupplementValidationError("公众号 URL 含非稳定访问参数")
    if channel == "maimai":
        raise SupplementValidationError("当前不接收未验活的脉脉链接")
    return normalize_url(url)


def _validate_summary(value: Any) -> str:
    summary = " ".join(str(value or "").split())
    folded = summary.casefold()
    if len(summary) < 30:
        raise SupplementValidationError("补充证据 summary 过短")
    if any(phrase in folded for phrase in TEMPLATE_PHRASES):
        raise SupplementValidationError("补充证据 summary 是登录或平台模板，不是正文")
    return summary[:600]


def _validated_source(channel: str, value: Any) -> str:
    base = CHANNEL_LABELS[channel]
    raw = " ".join(str(value or "").split())
    if not raw or raw == base:
        return base
    # Only a short author/publisher suffix may extend the controlled channel label.
    prefix = f"{base}｜"
    suffix = raw.removeprefix(prefix) if raw.startswith(prefix) else ""
    return f"{base}｜{suffix}" if suffix and SOURCE_SUFFIX_RE.fullmatch(suffix) else base


def default_agent_reach_coverage() -> tuple[SupplementCoverage, ...]:
    return (
        SupplementCoverage("xiaohongshu", "skipped", "未收到本机 Agent Reach 脱敏补充包，本轮未执行"),
        SupplementCoverage("wechat", "skipped", "未收到本机 Agent Reach 脱敏补充包，本轮未执行"),
        SupplementCoverage("maimai", "unsupported", "当前适配器不支持职位或职言搜索"),
        SupplementCoverage("boss", "auth_required", "未验证本机 BOSS 登录态；未实读 JD 不推送"),
    )


def parse_supplement(
    payload: Any,
    *,
    now: datetime | None = None,
    max_age_hours: int = 36,
) -> SupplementBundle:
    if not isinstance(payload, dict):
        raise SupplementValidationError("Agent Reach 补充包必须是 JSON 对象")
    _reject_sensitive_keys(payload)
    _require_known_keys(payload, ALLOWED_TOP_LEVEL_KEYS, "root")
    if payload.get("schema_version") != 1:
        raise SupplementValidationError("Agent Reach 补充包 schema_version 必须为 1")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated = _timestamp(payload.get("generated_at"), "generated_at")
    if generated > current + timedelta(minutes=5):
        raise SupplementValidationError("Agent Reach 补充包时间来自未来")
    if generated < current - timedelta(hours=max_age_hours):
        raise SupplementValidationError(f"Agent Reach 补充包已超过 {max_age_hours} 小时")

    raw_coverage = payload.get("coverage")
    if not isinstance(raw_coverage, list) or not raw_coverage:
        raise SupplementValidationError("coverage 必须是非空数组")
    coverage: list[SupplementCoverage] = []
    by_channel: dict[str, SupplementCoverage] = {}
    for index, entry in enumerate(raw_coverage):
        if not isinstance(entry, dict):
            raise SupplementValidationError(f"coverage[{index}] 必须是对象")
        _require_known_keys(entry, ALLOWED_COVERAGE_KEYS, f"coverage[{index}]")
        channel = str(entry.get("channel", "")).casefold()
        status = str(entry.get("status", "")).casefold()
        if channel not in CHANNEL_HOSTS:
            raise SupplementValidationError(f"coverage[{index}] channel 无效")
        if status not in ALLOWED_STATUSES:
            raise SupplementValidationError(f"coverage[{index}] status 无效")
        if channel in by_channel:
            raise SupplementValidationError(f"coverage 重复渠道: {channel}")
        row = SupplementCoverage(
            channel=channel,
            status=status,
            summary=" ".join(str(entry.get("summary", "")).split())[:180],
            queries=_nonnegative_int(entry.get("queries", 0), "queries"),
            raw_count=_nonnegative_int(entry.get("raw_count", 0), "raw_count"),
            relevant_count=_nonnegative_int(entry.get("relevant_count", 0), "relevant_count"),
            detail_reads=_nonnegative_int(entry.get("detail_reads", 0), "detail_reads"),
        )
        if row.relevant_count > row.raw_count or row.detail_reads > row.raw_count:
            raise SupplementValidationError(f"coverage[{index}] 计数不一致")
        if row.status in {"no_results", "auth_required", "unsupported", "error", "skipped"} and row.relevant_count:
            raise SupplementValidationError(f"coverage[{index}] 状态与 relevant_count 不一致")
        coverage.append(row)
        by_channel[channel] = row

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise SupplementValidationError("items 必须是数组")
    item_counts: dict[str, int] = {}
    detail_item_counts: dict[str, int] = {}
    signals: list[TrendSignal] = []
    identities: set[str] = set()
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            raise SupplementValidationError(f"items[{index}] 必须是对象")
        _require_known_keys(entry, ALLOWED_ITEM_KEYS, f"items[{index}]")
        channel = str(entry.get("channel", "")).casefold()
        kind = str(entry.get("kind", "")).casefold()
        coverage_row = by_channel.get(channel)
        if channel not in CHANNEL_HOSTS or kind not in {"content", "platform"}:
            raise SupplementValidationError(f"items[{index}] channel/kind 无效")
        if not coverage_row or coverage_row.status != "ok":
            raise SupplementValidationError(f"items[{index}] 缺少 ok coverage")
        evidence = str(entry.get("evidence", "")).casefold()
        if evidence not in {"detail_read", "search_summary"}:
            raise SupplementValidationError(f"items[{index}] evidence 无效")
        if kind == "platform" and evidence != "detail_read":
            raise SupplementValidationError("平台岗位必须完成正文实读")
        title = " ".join(str(entry.get("title", "")).split())
        if len(title) < 4:
            raise SupplementValidationError(f"items[{index}] 标题过短")
        summary = _validate_summary(entry.get("summary"))
        url = _validate_url(channel, entry.get("url"), kind)
        observed = _timestamp(entry.get("observed_at"), f"items[{index}].observed_at")
        if observed > generated + timedelta(minutes=5) or observed > current + timedelta(minutes=5):
            raise SupplementValidationError(f"items[{index}].observed_at 来自未来")
        if observed < generated - timedelta(hours=24):
            raise SupplementValidationError(f"items[{index}].observed_at 与补充包时间不一致")
        published_text = ""
        published_value = entry.get("published_at")
        if published_value not in (None, ""):
            published = _timestamp(published_value, f"items[{index}].published_at")
            if published > generated + timedelta(days=2):
                raise SupplementValidationError(f"items[{index}].published_at 来自未来")
            published_text = published.isoformat(timespec="seconds")
        signal = TrendSignal(
            title[:160],
            url,
            summary,
            _validated_source(channel, entry.get("source")),
            kind,
            observed.isoformat(timespec="seconds"),
            published_text,
        )
        if signal.identity in identities:
            raise SupplementValidationError(f"items[{index}] 与已有证据重复")
        identities.add(signal.identity)
        signals.append(signal)
        item_counts[channel] = item_counts.get(channel, 0) + 1
        if evidence == "detail_read":
            detail_item_counts[channel] = detail_item_counts.get(channel, 0) + 1

    for channel, count in item_counts.items():
        row = by_channel[channel]
        if row.relevant_count < count:
            raise SupplementValidationError(f"{channel} coverage.relevant_count 少于输出条数")
        if row.detail_reads < detail_item_counts.get(channel, 0):
            raise SupplementValidationError(f"{channel} coverage.detail_reads 少于实读证据条数")
    return SupplementBundle(1, generated.isoformat(timespec="seconds"), tuple(coverage), tuple(signals))


def load_supplement(
    path: Path,
    *,
    now: datetime | None = None,
    max_age_hours: int = 36,
) -> SupplementBundle:
    try:
        if path.stat().st_size > 256_000:
            raise SupplementValidationError("Agent Reach 补充包过大")
        raw_text = path.read_text(encoding="utf-8")
    except SupplementValidationError:
        raise
    except OSError as exc:
        raise SupplementValidationError(f"无法读取 Agent Reach 补充包: {exc}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SupplementValidationError("Agent Reach 补充包不是有效 JSON") from exc
    return parse_supplement(payload, now=now, max_age_hours=max_age_hours)
