"""Shared data types and URL primitives for the job radar."""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass


USER_AGENT = "SunnyJobRadar/1.0 (+https://github.com/gladiator19910312-sketch/daily-jd-push)"
HTTP_TIMEOUT_SECONDS = 20
MAX_MESSAGE_BYTES = 16_000


def is_public_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def is_stable_public_signal_url(value: str) -> bool:
    """Allow only non-session URLs that are safe to place in a report."""
    if not is_public_http_url(value):
        return False
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment:
        return False
    host = (parsed.hostname or "").casefold()
    if host in {"xiaohongshu.com", "www.xiaohongshu.com", "weixin.sogou.com"}:
        return False
    query_keys = {
        key.casefold().replace("-", "_")
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    }
    sensitive = {
        "access_token", "auth", "authorization", "cookie", "key", "openid",
        "pass_ticket", "signature", "sogou_token", "token", "xsec_token",
    }
    if any(key in sensitive or key.startswith("xsec_") for key in query_keys):
        return False
    if host == "mp.weixin.qq.com":
        allowed = {"__biz", "mid", "idx", "sn"}
        return (
            (parsed.path == "/s" or parsed.path.startswith("/s/"))
            and query_keys <= allowed
        )
    return True


def normalize_url(value: str) -> str:
    if not is_public_http_url(value):
        return ""
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").casefold()
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if host.endswith("linkedin.com") and "/jobs/view/" in parsed.path:
        kept_query: list[tuple[str, str]] = []
    elif host in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        kept_query = [
            (key, val)
            for key, val in query
            if not key.casefold().startswith(("xsec_", "utm_"))
            and key.casefold() not in {"source", "from"}
        ]
    elif host == "mp.weixin.qq.com":
        stable_keys = {"__biz", "mid", "idx", "sn"}
        kept_query = [(key, val) for key, val in query if key in stable_keys]
    else:
        tracking_keys = {
            "gh_src", "lever-source", "source", "src", "from", "referrer", "trk",
            "trackingid",
        }
        kept_query = [
            (key, val)
            for key, val in query
            if not key.casefold().startswith("utm_")
            and key.casefold() not in tracking_keys
        ]
    kept_query.sort(key=lambda item: (item[0].casefold(), item[1]))
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path, urllib.parse.urlencode(kept_query), "")
    )


@dataclass(frozen=True)
class Job:
    title: str
    url: str
    summary: str
    source: str
    job_id: str = ""
    location: str = "未披露"
    scope: str = "unknown"
    official: bool = False
    source_key: str = ""
    company: str = ""
    published_at: str = ""
    date_basis: str = "unknown"
    active: bool = False
    valid_through: str = ""

    @property
    def identity(self) -> str:
        if self.job_id:
            normalized = f"{(self.source_key or self.source).casefold()}:{self.job_id}"
        else:
            normalized = normalize_url(self.url) or self.title.casefold().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"


@dataclass(frozen=True)
class SourceCoverage:
    """One configured official source's outcome for the current scan."""

    source_key: str
    name: str
    scope: str
    status: str
    count: int
    diagnostic: str = ""


@dataclass(frozen=True)
class TrendQueryCoverage:
    """One public-index/content query's observable execution outcome."""

    name: str
    kind: str
    status: str
    raw_count: int
    accepted_count: int


@dataclass(frozen=True)
class Salary:
    total_low_wan: float | None = None
    total_high_wan: float | None = None
    fixed_low_wan: float | None = None
    fixed_high_wan: float | None = None
    label: str = "未披露"
    annual_cash_low_wan: float | None = None
    annual_cash_high_wan: float | None = None


@dataclass(frozen=True)
class Assessment:
    job: Job
    fit: int
    ready: int
    asset: str
    salary: Salary
    salary_gate: str
    responsibilities: tuple[str, ...]
    strengths: tuple[str, ...]
    gaps: tuple[str, ...]
    work_risk: str
    excluded_reason: str | None = None
    required_experience_years: int | None = None

    @property
    def eligible(self) -> bool:
        return self.excluded_reason is None


@dataclass(frozen=True)
class TrendSignal:
    title: str
    url: str
    summary: str
    source: str
    kind: str
    indexed_at: str
    published_at: str = ""

    @property
    def identity(self) -> str:
        normalized = normalize_url(self.url) or self.title.casefold().strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
