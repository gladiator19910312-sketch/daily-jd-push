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
    suffix = "\n\n内容过长，已截断。".encode("utf-8")
    prefix = encoded[: max_bytes - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix.decode("utf-8")


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


COVERAGE_LABELS = {
    "boss": "BOSS 直聘",
    "linkedin": "LinkedIn",
    "maimai": "脉脉",
    "wechat": "微信公众号",
    "xiaohongshu": "小红书",
}
COVERAGE_STATUSES = {
    "ok": "已执行",
    "no_results": "已执行，无合格结果",
    "auth_required": "需登录 / 未验活",
    "unsupported": "当前不支持",
    "error": "执行失败",
    "skipped": "本轮未执行",
}


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
    config = config or {}
    trend_items = trend_items or []
    signals = signals or []
    timezone = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
    now = datetime.now(timezone)
    lines = [
        f"## Sunny 每日 Agent 岗位雷达｜{now:%Y-%m-%d}",
        "",
        "## 北京 / 天津｜社招优先可行动岗位",
        "",
    ]
    if not items:
        lines.extend(["今天没有发现新的、通过时效与职业红线的北京/天津岗位。", ""])
    for index, item in enumerate(items, 1):
        decision = "强匹配" if item.fit >= 85 and item.ready >= 65 else "值得核验"
        freshness = job_freshness(item.job, config, now)
        lines.extend(
            [
                f"### {index}. 【{decision}】{markdown_link(item.job.title, item.job.url)}",
                f"- **地点 / 来源：** {markdown_text(item.job.location)}｜{markdown_text(item.job.source)}",
                f"- **岗位时效：** {markdown_text(freshness.label)}",
                f"- **岗位重点：** {'；'.join(markdown_text(value) for value in item.responsibilities)}",
                f"- **Fit / Ready：{item.fit} / {item.ready}｜两年资产：{item.asset}**",
                f"- **优势：** {'；'.join(markdown_text(value) for value in item.strengths)}",
                f"- **关键缺口：** {'；'.join(markdown_text(value) for value in item.gaps)}",
                f"- **薪酬口径：** {markdown_text(item.salary.label)}；{markdown_text(item.salary_gate)}",
                f"- **强度/差旅：** {markdown_text(item.work_risk)}",
                "",
            ]
        )

    if trend_items:
        lines.extend(["## 非主推｜其他城市 / 海外 / 时效待核验", ""])
        for item in trend_items:
            freshness = job_freshness(item.job, config, now)
            lines.append(
                f"- {markdown_link(item.job.title, item.job.url)}｜{markdown_text(item.job.location)}｜"
                f"{markdown_text(item.job.source)}｜Fit/Ready {item.fit}/{item.ready}｜"
                f"{markdown_text(freshness.label)}｜{markdown_text(item.responsibilities[0])}"
            )
        lines.append("")

    if source_coverage:
        lines.extend(format_source_coverage(source_coverage))

    platform_signals = [signal for signal in signals if signal.kind == "platform"]
    content_signals = [signal for signal in signals if signal.kind != "platform"]
    lines.extend(["## 社招高阶线索｜招聘平台 / 公共就业 / 人才网", ""])
    if platform_signals:
        lines.extend(
            [
                "以下为公开索引线索，不等同于企业官网在招；投递前需回企业招聘页或联系招聘方二次验活。",
                "",
            ]
        )
        for signal in platform_signals:
            indexed = parse_timestamp(signal.indexed_at)
            indexed_label = (
                indexed.astimezone(timezone).date().isoformat()
                if indexed
                else "发现日期未知"
            )
            published = parse_timestamp(signal.published_at)
            published_label = (
                f"原文日期 {published.astimezone(timezone).date().isoformat()}"
                if published
                else "原文日期未披露"
            )
            lines.append(
                f"- {markdown_link(signal.title, signal.url if is_stable_public_signal_url(signal.url) else '')}｜"
                f"{markdown_text(signal.source)}｜社招岗位线索｜"
                f"本次验活 {indexed_label}，{published_label}｜{markdown_text(signal_excerpt(signal))}"
            )
        lines.append("")
    else:
        lines.extend(
            [
                "本轮未输出任何未经正文验活的平台链接；搜索聚合页、SEO 模板页、安全验证页和空正文一律抑制。",
                "",
            ]
        )

    lines.extend(["## 行业报告 / 公众号 / 小红书｜趋势参考", ""])
    if content_signals:
        for signal in content_signals:
            indexed = parse_timestamp(signal.indexed_at)
            indexed_label = (
                indexed.astimezone(timezone).date().isoformat()
                if indexed
                else "发现日期未知"
            )
            published = parse_timestamp(signal.published_at)
            published_label = (
                f"原文日期 {published.astimezone(timezone).date().isoformat()}"
                if published
                else "原文日期未披露"
            )
            lines.append(
                f"- {markdown_link(signal.title, signal.url if is_stable_public_signal_url(signal.url) else '')}｜"
                f"{markdown_text(signal.source)}｜"
                f"本次采集/索引 {indexed_label}，{published_label}｜{markdown_text(signal_excerpt(signal))}"
            )
        lines.append("")
    else:
        lines.extend(["本轮没有新的、可公开验证的行业内容信号。", ""])

    themes = Counter(
        responsibility
        for item in [*items, *trend_items]
        for responsibility in item.responsibilities
    )
    if themes:
        lines.append(
            "**本轮需求信号：** "
            + "；".join(markdown_text(name) for name, _ in themes.most_common(3))
        )
    lines.append(
        f"本轮发现 {discovered_count} 条公开岗位；主推需明确位于北京/天津并仍在招，"
        "90 天内优先，91–180 天明确提示复核，超过 180 天剔除。"
    )
    if failures:
        lines.append(
            f"部分来源无结果或失败（共 {len(failures)} 项）："
            f"{'；'.join(markdown_text(value) for value in failures[:3])}。"
            "其他来源继续独立执行。"
        )
    lines.append(
        "企业官方 ATS / 招聘站用于可行动岗位；本机登录态渠道仅在上方覆盖表显示为“已执行”时才算本轮实际检索。"
        "社交内容只作未经企业背书的行业证据，未回企业官网验活的内容不会被当作可申请岗位。"
    )
    lines.append("薪酬、双休、21点后工作频率和差旅以招聘方书面确认及面试反向背调为准。")
    return truncate_utf8("\n".join(lines), MAX_MESSAGE_BYTES)


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


def send_dingtalk(markdown: str, webhook: str, secret: str) -> None:
    validate_dingtalk_webhook(webhook)
    payload = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"title": "Sunny 每日岗位雷达", "text": truncate_utf8(markdown, MAX_MESSAGE_BYTES)},
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
