"""Network discovery across configured official ATS feeds and search RSS."""

from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from radar_ats import parse_ashby, parse_greenhouse, parse_lever, parse_moka, strip_html
from radar_matching import looks_like_product_job
from radar_types import HTTP_TIMEOUT_SECONDS, USER_AGENT, Job, is_public_http_url


TRUSTED_JOB_HOSTS = {
    "app.mokahr.com", "jobs.ashbyhq.com", "job-boards.greenhouse.io",
    "boards.greenhouse.io", "jobs.lever.co", "www.google.com", "google.com",
    "jobs.apple.com", "www.amazon.jobs", "amazon.jobs",
}


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/rss+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == 2:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 8))
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def url_with_query(url: str, **updates: Any) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in updates.items()})
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def fetch_official_source(source: dict[str, Any]) -> list[Job]:
    source_type = str(source["type"])
    if source_type == "greenhouse":
        return parse_greenhouse(http_get(str(source["url"])), source)
    if source_type == "ashby":
        return parse_ashby(http_get(str(source["url"])), source)
    if source_type == "lever":
        return parse_lever(http_get(str(source["url"])), source)
    if source_type == "moka":
        limit, offset = int(source.get("page_size", 100)), 0
        jobs: list[Job] = []
        while True:
            payload = http_get(url_with_query(str(source["url"]), limit=limit, offset=offset))
            page_jobs, total = parse_moka(payload, source)
            jobs.extend(page_jobs)
            offset += limit
            if offset >= total:
                return jobs
    raise ValueError(f"unsupported source type: {source_type}")


def parse_rss(payload: bytes, source: str, limit: int) -> list[Job]:
    root = ET.fromstring(payload)
    jobs: list[Job] = []
    for item in root.findall(".//item")[:limit]:
        title = html.unescape((item.findtext("title") or "").strip())
        url = (item.findtext("link") or "").strip()
        summary = strip_html(item.findtext("description") or "")
        if title and is_public_http_url(url):
            jobs.append(Job(title, url, summary, source))
    return jobs


def is_trusted_job_url(value: str, extra_hosts: Iterable[str] = ()) -> bool:
    if not is_public_http_url(value):
        return False
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").casefold()
    allowed = TRUSTED_JOB_HOSTS | {item.casefold() for item in extra_hosts}
    if host not in allowed:
        return False
    if host in {"google.com", "www.google.com"}:
        return parsed.path.startswith("/about/careers/applications/jobs/")
    return True


def bing_rss_url(query: str, language: str) -> str:
    params = urllib.parse.urlencode({"format": "rss", "setlang": language, "q": query})
    return f"https://www.bing.com/search?{params}"


def discover_jobs(config: dict[str, Any]) -> tuple[list[Job], list[str]]:
    discovered: dict[str, Job] = {}
    failures: list[str] = []
    for source in config.get("official_sources", []):
        try:
            for job in fetch_official_source(source):
                discovered[job.identity] = job
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
            failures.append(f"{source.get('name', 'official')}: {type(exc).__name__}")

    limit = int(config.get("max_results_per_query", 12))
    trusted_hosts = config.get("trusted_job_hosts", [])
    for query in config.get("queries", []):
        try:
            payload = http_get(bing_rss_url(query["query"], query.get("language", "en-US")))
            for job in parse_rss(payload, query["name"], limit):
                if not is_trusted_job_url(job.url, trusted_hosts):
                    continue
                candidate = Job(
                    job.title,
                    job.url,
                    job.summary,
                    f"{query['name']} · 搜索索引待验活",
                    scope=str(query.get("scope", "unknown")),
                )
                discovered.setdefault(candidate.identity, candidate)
        except (ET.ParseError, OSError, urllib.error.URLError) as exc:
            failures.append(f"{query['name']}: {type(exc).__name__}")
    return list(discovered.values()), failures


def enrich_jobs(jobs: Iterable[Job], max_fetches: int) -> list[Job]:
    enriched: list[Job] = []
    fetched = 0
    for job in jobs:
        if job.official or fetched >= max_fetches:
            enriched.append(job)
            continue
        fetched += 1
        try:
            page = http_get(job.url)[:350_000].decode("utf-8", errors="ignore")
            body = strip_html(page)
            if len(body) >= 120:
                enriched.append(
                    Job(
                        job.title, job.url, f"{job.summary} {body[:12_000]}", job.source,
                        job.job_id, job.location, job.scope, job.official, job.source_key,
                    )
                )
                continue
        except (OSError, UnicodeError, urllib.error.URLError):
            pass
        enriched.append(job)
    return enriched
