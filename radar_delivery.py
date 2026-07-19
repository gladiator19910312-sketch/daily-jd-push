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
from datetime import datetime
from pathlib import Path
from typing import Any

from radar_types import HTTP_TIMEOUT_SECONDS, MAX_MESSAGE_BYTES, USER_AGENT, Assessment


def load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("job_ids", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "job_ids": sorted(seen)[-2000:]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = "\n\n内容过长，已截断。".encode("utf-8")
    prefix = encoded[: max_bytes - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix.decode("utf-8")


def format_report(items: list[Assessment], discovered_count: int, failures: list[str]) -> str:
    lines = [f"## Sunny 每日 Agent 岗位雷达｜{datetime.now():%Y-%m-%d}", ""]
    if not items:
        lines.extend(["今天没有发现同时通过 Fit、Ready 与薪酬公开红线的新岗位。", ""])
    for index, item in enumerate(items, 1):
        decision = "强匹配" if item.fit >= 85 and item.ready >= 65 else "值得核验"
        lines.extend(
            [
                f"### {index}. 【{decision}】[{item.job.title}]({item.job.url})",
                f"- **地点 / 来源：** {item.job.location}｜{item.job.source}",
                f"- **岗位重点：** {'；'.join(item.responsibilities)}",
                f"- **Fit / Ready：{item.fit} / {item.ready}｜两年资产：{item.asset}**",
                f"- **优势：** {'；'.join(item.strengths)}",
                f"- **关键缺口：** {'；'.join(item.gaps)}",
                f"- **薪酬口径：** {item.salary.label}；{item.salary_gate}",
                f"- **强度/差旅：** {item.work_risk}",
                "",
            ]
        )
    lines.append(
        f"本轮发现 {discovered_count} 条公开结果；只推送 Fit≥72、Ready≥48 且没有明确身份/薪酬硬冲突的岗位。"
    )
    if failures:
        lines.append(f"部分来源失败：{'；'.join(failures[:3])}。其余来源仍已完成。")
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
