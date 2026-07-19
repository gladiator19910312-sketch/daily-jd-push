"""Network discovery across configured official ATS feeds and search RSS."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from radar_ats import parse_ashby, parse_greenhouse, parse_lever, parse_moka, parse_tencent, strip_html
from radar_matching import looks_like_product_job
from radar_search import duckduckgo_lite_url, parse_duckduckgo_results
from radar_types import USER_AGENT, Job, is_public_http_url


TRUSTED_JOB_HOSTS = {
    "app.mokahr.com", "jobs.ashbyhq.com", "job-boards.greenhouse.io",
    "boards.greenhouse.io", "jobs.lever.co", "www.google.com", "google.com",
    "jobs.apple.com", "www.amazon.jobs", "amazon.jobs",
}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def http_get(
    url: str,
    *,
    follow_redirects: bool = True,
    timeout_seconds: int = 12,
    attempts: int = 2,
) -> bytes:
    attempts = max(1, attempts)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/rss+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler() if follow_redirects else NoRedirectHandler()
    )
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 8))
        except urllib.error.URLError:
            if attempt == attempts - 1:
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
    if source_type == "tencent":
        page_size = int(source.get("page_size", 100))
        jobs: dict[str, Job] = {}
        for keyword in source.get("keywords", []):
            page = 1
            while True:
                url = str(source["url_template"]).format(
                    keyword=urllib.parse.quote(str(keyword)),
                    page=page,
                    page_size=page_size,
                )
                page_jobs, total = parse_tencent(http_get(url), source)
                jobs.update((job.identity, job) for job in page_jobs)
                if page * page_size >= total:
                    break
                page += 1
        return list(jobs.values())
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
        candidates: list[Job] = []
        primary_error: Exception | None = None
        try:
            payload = http_get(
                duckduckgo_lite_url(query["query"], query.get("language", "en-US")),
                timeout_seconds=6,
                attempts=1,
            )
            for result in parse_duckduckgo_results(payload, limit):
                candidate = Job(
                    result.title,
                    result.url,
                    result.summary,
                    f"{query['name']} · 搜索索引待验活",
                    scope=str(query.get("scope", "unknown")),
                )
                if is_trusted_job_url(candidate.url, trusted_hosts) and looks_like_product_job(candidate):
                    candidates.append(candidate)
        except (KeyError, OSError, urllib.error.URLError) as exc:
            primary_error = exc

        if not candidates:
            try:
                payload = http_get(
                    bing_rss_url(query["query"], query.get("language", "en-US")),
                    timeout_seconds=6,
                    attempts=1,
                )
                for job in parse_rss(payload, query["name"], limit):
                    candidate = Job(
                        job.title,
                        job.url,
                        job.summary,
                        f"{query['name']} · 搜索索引待验活",
                        scope=str(query.get("scope", "unknown")),
                    )
                    if is_trusted_job_url(candidate.url, trusted_hosts) and looks_like_product_job(candidate):
                        candidates.append(candidate)
            except (ET.ParseError, KeyError, OSError, urllib.error.URLError) as exc:
                error = primary_error or exc
                failures.append(f"{query.get('name', 'search')}: {type(error).__name__}")
        if not candidates and not any(
            failure.startswith(f"{query.get('name', 'search')}:") for failure in failures
        ):
            failures.append(f"{query.get('name', 'search')}: 未发现可验活结果")
        for candidate in candidates:
            discovered.setdefault(candidate.identity, candidate)
    return list(discovered.values()), failures


def json_ld_job_posting(page: str) -> dict[str, Any]:
    blocks = re.findall(
        r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page,
    )

    def find_posting(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            kinds = value.get("@type", ())
            if isinstance(kinds, str):
                kinds = (kinds,)
            if "JobPosting" in kinds:
                return value
            for nested in value.values():
                found = find_posting(nested)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = find_posting(nested)
                if found:
                    return found
        return None

    for block in blocks:
        try:
            found = find_posting(json.loads(html.unescape(block).strip()))
        except json.JSONDecodeError:
            continue
        if found:
            return found
    return {}


def posting_location(posting: dict[str, Any], body: str) -> str:
    raw_locations = posting.get("jobLocation")
    if isinstance(raw_locations, dict):
        raw_locations = [raw_locations]
    places: list[str] = []
    if isinstance(raw_locations, list):
        for raw in raw_locations:
            if not isinstance(raw, dict):
                continue
            address = raw.get("address") if isinstance(raw.get("address"), dict) else raw
            place = "·".join(
                str(address.get(key)).strip()
                for key in ("addressCountry", "addressRegion", "addressLocality")
                if address.get(key)
            )
            if place:
                places.append(place)
    if places:
        return " / ".join(dict.fromkeys(places))
    detected = [city for city in ("北京市", "天津市") if city in body[:1800]]
    return " / ".join(detected) or "未披露"


def page_date(posting: dict[str, Any]) -> tuple[str, str]:
    if posting.get("datePosted"):
        return str(posting["datePosted"]), "published"
    return "", "unknown"


def page_is_closed(body: str) -> bool:
    folded = body.casefold()
    return any(
        marker in folded
        for marker in (
            "职位已下线", "职位已关闭", "职位已过期", "停止招聘", "已停止接受求职申请",
            "no longer accepting applications", "job has expired", "position has been filled",
        )
    )


def enrich_jobs(
    jobs: Iterable[Job],
    max_fetches: int,
    official_hosts: Iterable[str] = (),
) -> list[Job]:
    enriched: list[Job] = []
    fetched = 0
    allowed_hosts = TRUSTED_JOB_HOSTS | {host.casefold() for host in official_hosts}
    for job in jobs:
        if job.official or fetched >= max_fetches:
            enriched.append(job)
            continue
        host = (urllib.parse.urlsplit(job.url).hostname or "").casefold()
        if host not in allowed_hosts:
            enriched.append(job)
            continue
        fetched += 1
        try:
            page = http_get(
                job.url,
                follow_redirects=False,
                timeout_seconds=10,
                attempts=1,
            )[:350_000].decode("utf-8", errors="ignore")
            body = strip_html(page)
            if len(body) >= 120 and not page_is_closed(body):
                posting = json_ld_job_posting(page)
                if not posting:
                    enriched.append(job)
                    continue
                title = str(posting.get("title") or job.title).strip()
                description = strip_html(str(posting.get("description") or ""))
                if not title or not description:
                    enriched.append(job)
                    continue
                published_at, date_basis = page_date(posting)
                organization = posting.get("hiringOrganization")
                company = str(organization.get("name") or "") if isinstance(organization, dict) else ""
                enriched.append(
                    Job(
                        title=title,
                        url=job.url,
                        summary=description,
                        source=f"{job.source} · 官网已验活",
                        job_id=job.job_id,
                        location=posting_location(posting, body),
                        scope=job.scope,
                        official=True,
                        source_key=job.source_key or f"official:{host}",
                        company=company,
                        published_at=published_at,
                        date_basis=date_basis,
                        active=True,
                        valid_through=str(posting.get("validThrough") or ""),
                    )
                )
                continue
        except (OSError, UnicodeError, urllib.error.URLError):
            pass
        enriched.append(job)
    return enriched
