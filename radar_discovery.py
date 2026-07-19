"""Network discovery across configured official ATS feeds and search RSS."""

from __future__ import annotations

import html
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from radar_ats import (
    ClosedJobError,
    parse_alibaba,
    parse_amazon,
    parse_ashby,
    parse_bytedance,
    parse_greenhouse,
    parse_lever,
    parse_meituan,
    parse_meituan_detail,
    parse_microsoft_detail,
    parse_microsoft_search,
    parse_moka,
    parse_tencent,
    strip_html,
)
from radar_matching import looks_like_candidate_job
from radar_search import duckduckgo_lite_url, parse_duckduckgo_results
from radar_types import USER_AGENT, Job, SourceCoverage, is_public_http_url


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


def http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: int = 12,
    attempts: int = 2,
    headers: dict[str, str] | None = None,
    opener: Any = None,
) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method="POST",
    )
    client = opener or urllib.request.build_opener()
    for attempt in range(max(1, attempts)):
        try:
            with client.open(request, timeout=timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts - 1:
                raise
            time.sleep(min(2**attempt, 8))
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def http_get_with_opener(
    url: str,
    opener: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 12,
) -> bytes:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with opener.open(request, timeout=timeout_seconds) as response:
        return response.read()


def url_with_query(url: str, **updates: Any) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in updates.items()})
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def fetch_official_source(
    source: dict[str, Any],
    max_seconds: float = 60,
    warnings: list[str] | None = None,
) -> list[Job]:
    deadline = time.monotonic() + max(5.0, float(max_seconds))
    scan_warnings = warnings if warnings is not None else []
    source_name = str(source.get("name", "official"))

    def out_of_time() -> bool:
        return time.monotonic() >= deadline

    def remaining_timeout() -> int:
        return max(1, min(12, int(max(1.0, deadline - time.monotonic()))))

    def warn_once(message: str) -> None:
        if message not in scan_warnings:
            scan_warnings.append(message)

    source_type = str(source["type"])
    if source_type == "bytedance":
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        referer = "https://jobs.bytedance.com/experienced/position"
        token_data = json.loads(
            http_post_json(
                str(source["token_url"]),
                {"portal_entrance": 1},
                headers={"Referer": referer},
                opener=opener,
            ).decode("utf-8")
        )
        raw_token = token_data.get("data") if isinstance(token_data, dict) else None
        token = raw_token.get("token") if isinstance(raw_token, dict) else raw_token
        if not isinstance(token, str) or not token:
            raise ValueError("ByteDance CSRF token is missing")
        page_size = int(source.get("page_size", 20))
        max_pages = int(source.get("max_pages_per_keyword", 2))
        jobs: dict[str, Job] = {}
        last_error: Exception | None = None
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            try:
                for page in range(max_pages):
                    if out_of_time():
                        break
                    payload = {
                        "job_category_id_list": [],
                        "keyword": str(keyword),
                        "limit": page_size,
                        "location_code_list": [],
                        "offset": page * page_size,
                        "portal_entrance": 1,
                        "portal_type": 2,
                        "recruitment_id_list": [],
                        "subject_id_list": [],
                    }
                    page_jobs, total = parse_bytedance(
                        http_post_json(
                            str(source["url"]),
                            payload,
                            headers={"Referer": referer, "x-csrf-token": token},
                            opener=opener,
                        ),
                        source,
                    )
                    jobs.update((job.identity, job) for job in page_jobs)
                    if (page + 1) * page_size >= total:
                        break
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                continue
        if not jobs and last_error:
            raise last_error
        if jobs and last_error:
            warn_once(f"{source_name}: 部分关键词请求失败，已保留其余结果")
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(jobs.values())
    if source_type == "alibaba":
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        referer = "https://talent.alibaba.com/off-campus/position-list?lang=zh"
        http_get_with_opener(referer, opener)
        token = next(
            (urllib.parse.unquote(cookie.value) for cookie in cookie_jar if cookie.name == "XSRF-TOKEN"),
            "",
        )
        if not token:
            raise ValueError("Alibaba XSRF token is missing")
        url = url_with_query(str(source["url"]), _csrf=token)
        page_size = int(source.get("page_size", 20))
        max_pages = int(source.get("max_pages_per_keyword", 2))
        jobs: dict[str, Job] = {}
        last_error: Exception | None = None
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            try:
                for page in range(1, max_pages + 1):
                    if out_of_time():
                        break
                    payload = {
                        "batchId": "",
                        "categories": "",
                        "channel": "group_official_site",
                        "deptCodes": [],
                        "key": str(keyword),
                        "language": "zh",
                        "pageIndex": page,
                        "pageSize": page_size,
                        "regions": "",
                        "subCategories": "",
                    }
                    page_jobs, total = parse_alibaba(
                        http_post_json(
                            url,
                            payload,
                            headers={"Referer": referer, "Origin": "https://talent.alibaba.com"},
                            opener=opener,
                        ),
                        source,
                    )
                    jobs.update((job.identity, job) for job in page_jobs)
                    if page * page_size >= total:
                        break
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                continue
        if not jobs and last_error:
            raise last_error
        if jobs and last_error:
            warn_once(f"{source_name}: 部分关键词请求失败，已保留其余结果")
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(jobs.values())
    if source_type == "microsoft":
        page_size = int(source.get("page_size", 10))
        max_pages = int(source.get("max_pages_per_keyword", 2))
        candidates: dict[str, Job] = {}
        last_error: Exception | None = None
        successful_pages = 0
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            try:
                for page in range(max_pages):
                    if out_of_time():
                        break
                    url = url_with_query(
                        str(source["url"]),
                        domain=str(source.get("domain", "microsoft.com")),
                        query=str(keyword),
                        location=str(source.get("location", "China, Beijing")),
                        start=page * page_size,
                        sort_by=str(source.get("sort_by", "timestamp")),
                        hl=str(source.get("language", "en")),
                    )
                    page_jobs, total = parse_microsoft_search(
                        http_get(url, timeout_seconds=remaining_timeout(), attempts=1),
                        source,
                    )
                    successful_pages += 1
                    candidates.update((job.identity, job) for job in page_jobs)
                    if (page + 1) * page_size >= total:
                        break
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                continue
        if not successful_pages and last_error:
            raise last_error
        if last_error:
            warn_once(f"{source_name}: 部分搜索请求失败，已保留其余结果")

        detail_limit = int(source.get("max_detail_fetches", 12))
        detailed: dict[str, Job] = {}
        detail_errors = 0
        for candidate in list(candidates.values())[:detail_limit]:
            if out_of_time():
                break
            detail_url = url_with_query(
                str(source["detail_url"]),
                position_id=candidate.job_id,
                domain=str(source.get("domain", "microsoft.com")),
                hl=str(source.get("language", "en")),
            )
            try:
                detail = parse_microsoft_detail(
                    http_get(
                        detail_url,
                        timeout_seconds=remaining_timeout(),
                        attempts=1,
                    ),
                    source,
                    candidate.job_id,
                )
            except ClosedJobError:
                continue
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                urllib.error.URLError,
            ):
                detail_errors += 1
                continue
            detailed[detail.identity] = detail
        if detail_errors:
            warn_once(f"{source_name}: {detail_errors} 条候选岗位详情验活失败，未进入结果")
        if len(candidates) > detail_limit:
            warn_once(f"{source_name}: 达到详情验活上限，结果可能不完整")
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(detailed.values())
    if source_type == "amazon":
        page_size = int(source.get("page_size", 20))
        max_pages = int(source.get("max_pages_per_keyword", 1))
        jobs: dict[str, Job] = {}
        last_error: Exception | None = None
        successful_pages = 0
        truncated_keywords = 0
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            pages_fetched = 0
            keyword_total = 0
            try:
                for page in range(max_pages):
                    if out_of_time():
                        break
                    url = url_with_query(
                        str(source["url"]),
                        base_query=str(keyword),
                        loc_query=str(source.get("loc_query", "China")),
                        offset=page * page_size,
                        result_limit=page_size,
                        sort=str(source.get("sort", "relevant")),
                    )
                    page_jobs, total = parse_amazon(
                        http_get(url, timeout_seconds=remaining_timeout(), attempts=1),
                        source,
                    )
                    successful_pages += 1
                    pages_fetched += 1
                    keyword_total = total
                    jobs.update((job.identity, job) for job in page_jobs)
                    if (page + 1) * page_size >= total:
                        break
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
                continue
            if keyword_total > pages_fetched * page_size:
                truncated_keywords += 1
        if not successful_pages and last_error:
            raise last_error
        if last_error:
            warn_once(f"{source_name}: 部分关键词请求失败，已保留其余结果")
        if truncated_keywords:
            warn_once(
                f"{source_name}: {truncated_keywords} 个关键词达到分页上限，结果可能不完整"
            )
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(jobs.values())
    if source_type == "greenhouse":
        return parse_greenhouse(http_get(str(source["url"])), source)
    if source_type == "ashby":
        return parse_ashby(http_get(str(source["url"])), source)
    if source_type == "lever":
        return parse_lever(http_get(str(source["url"])), source)
    if source_type == "moka":
        limit, offset = int(source.get("page_size", 100)), 0
        max_pages = int(source.get("max_pages", 3))
        jobs: list[Job] = []
        for _ in range(max_pages):
            if out_of_time():
                break
            payload = http_get(url_with_query(str(source["url"]), limit=limit, offset=offset))
            page_jobs, total = parse_moka(payload, source)
            jobs.extend(page_jobs)
            offset += limit
            if offset >= total:
                break
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return jobs
    if source_type == "tencent":
        page_size = int(source.get("page_size", 100))
        max_pages = int(source.get("max_pages_per_keyword", 2))
        jobs: dict[str, Job] = {}
        last_error: Exception | None = None
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            try:
                for page in range(1, max_pages + 1):
                    if out_of_time():
                        break
                    url = str(source["url_template"]).format(
                        keyword=urllib.parse.quote(str(keyword)),
                        page=page,
                        page_size=page_size,
                    )
                    page_jobs, total = parse_tencent(http_get(url), source)
                    jobs.update((job.identity, job) for job in page_jobs)
                    if page * page_size >= total:
                        break
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                continue
        if not jobs and last_error:
            raise last_error
        if jobs and last_error:
            warn_once(f"{source_name}: 部分关键词请求失败，已保留其余结果")
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(jobs.values())
    if source_type == "meituan":
        page_size = int(source.get("page_size", 30))
        max_pages = int(source.get("max_pages_per_keyword", 2))
        jobs: dict[str, Job] = {}
        detail_limit = int(source.get("max_detail_fetches", 24))
        last_error: Exception | None = None
        for keyword in source.get("keywords", []):
            if out_of_time():
                break
            try:
                for page in range(1, max_pages + 1):
                    if out_of_time():
                        break
                    payload = {
                        "page": {"pageNo": page, "pageSize": page_size},
                        "jobShareType": "1",
                        "keywords": str(keyword),
                        "cityList": source.get("city_codes", []),
                        "department": [],
                        "jfJgList": [],
                        "jobType": [{"code": "3", "subCode": []}],
                        "typeCode": [],
                        "specialCode": [],
                    }
                    page_jobs, total = parse_meituan(
                        http_post_json(
                            str(source["url"]),
                            payload,
                            headers={"Referer": "https://zhaopin.meituan.com/web/social"},
                        ),
                        source,
                    )
                    jobs.update((job.identity, job) for job in page_jobs)
                    if page * page_size >= total:
                        break
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                continue
        if not jobs and last_error:
            raise last_error
        detailed: dict[str, Job] = {}
        closed_identities: set[str] = set()
        for job in list(jobs.values())[:detail_limit]:
            if out_of_time():
                break
            try:
                detail = parse_meituan_detail(
                    http_post_json(
                        str(source["detail_url"]),
                        {"jobUnionId": job.job_id, "jobShareType": "1"},
                        headers={"Referer": job.url},
                        attempts=1,
                    ),
                    source,
                )
            except ClosedJobError:
                closed_identities.add(job.identity)
                continue
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
                detail = None
            detailed[job.identity] = detail or job
        detailed.update(
            (identity, job)
            for identity, job in jobs.items()
            if identity not in detailed and identity not in closed_identities
        )
        if jobs and last_error:
            warn_once(f"{source_name}: 部分关键词请求失败，已保留其余结果")
        if out_of_time():
            warn_once(f"{source_name}: 达到来源时间预算，结果可能不完整")
        return list(detailed.values())
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


def discover_jobs_with_coverage(
    config: dict[str, Any],
) -> tuple[list[Job], list[str], tuple[SourceCoverage, ...]]:
    discovered: dict[str, Job] = {}
    failures: list[str] = []
    coverage: list[SourceCoverage] = []
    official_sources = list(config.get("official_sources", []))
    official_attempted = 0
    official_succeeded = 0
    official_deadline = time.monotonic() + float(
        config.get("official_discovery_budget_seconds", 360)
    )
    source_budget = float(config.get("official_source_budget_seconds", 60))
    for source_index, source in enumerate(official_sources):
        remaining = official_deadline - time.monotonic()
        if remaining <= 0:
            failures.append("企业官网扫描：达到本轮时间预算，剩余来源本轮未覆盖")
            for skipped_index, skipped in enumerate(
                official_sources[source_index:],
                start=source_index,
            ):
                coverage.append(
                    SourceCoverage(
                        str(skipped.get("key") or f"{skipped.get('type', 'official')}:{skipped_index}"),
                        str(skipped.get("name", "official")),
                        str(skipped.get("scope", "unknown")),
                        "skipped",
                        0,
                        "本轮企业官网总时间预算已用尽",
                    )
                )
            break
        official_attempted += 1
        try:
            source_warnings: list[str] = []
            source_jobs = {
                job.identity: job
                for job in fetch_official_source(
                    source,
                    min(source_budget, remaining),
                    source_warnings,
                )
            }
            for job in source_jobs.values():
                discovered[job.identity] = job
            failures.extend(source_warnings)
            if not source_warnings:
                official_succeeded += 1
            diagnostic = "；".join(source_warnings)
            if not diagnostic:
                diagnostic = "扫描成功" if source_jobs else "扫描成功，未发现目标岗位"
            coverage.append(
                SourceCoverage(
                    str(source.get("key") or f"{source.get('type', 'official')}:{source_index}"),
                    str(source.get("name", "official")),
                    str(source.get("scope", "unknown")),
                    "partial" if source_warnings else "ok",
                    len(source_jobs),
                    diagnostic,
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, urllib.error.URLError) as exc:
            failures.append(f"{source.get('name', 'official')}: {type(exc).__name__}")
            coverage.append(
                SourceCoverage(
                    str(source.get("key") or f"{source.get('type', 'official')}:{source_index}"),
                    str(source.get("name", "official")),
                    str(source.get("scope", "unknown")),
                    "error",
                    0,
                    type(exc).__name__,
                )
            )
    if official_attempted and official_succeeded * 2 < official_attempted:
        failures.insert(
            0,
            f"来源健康告警：企业官网成功 {official_succeeded}/{official_attempted}",
        )

    limit = int(config.get("max_results_per_query", 12))
    trusted_hosts = config.get("trusted_job_hosts", [])
    search_deadline = time.monotonic() + float(config.get("job_search_budget_seconds", 180))
    for query in config.get("queries", []):
        if time.monotonic() >= search_deadline:
            failures.append("企业官网搜索兜底：达到本轮时间预算，剩余查询本轮未覆盖")
            break
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
                if is_trusted_job_url(candidate.url, trusted_hosts) and looks_like_candidate_job(candidate):
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
                    if is_trusted_job_url(candidate.url, trusted_hosts) and looks_like_candidate_job(candidate):
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
    return list(discovered.values()), failures, tuple(coverage)


def discover_jobs(config: dict[str, Any]) -> tuple[list[Job], list[str]]:
    """Backward-compatible discovery API for existing callers."""
    jobs, failures, _coverage = discover_jobs_with_coverage(config)
    return jobs, failures


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
    employer_hosts: Iterable[str] = (),
) -> list[Job]:
    enriched: list[Job] = []
    fetched = 0
    allowed_hosts = TRUSTED_JOB_HOSTS | {host.casefold() for host in employer_hosts}
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
                        source=f"{job.source} · 企业招聘页已验活",
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
