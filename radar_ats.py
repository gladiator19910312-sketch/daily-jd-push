"""Pure parsers for official ATS payloads."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable

from radar_market import parse_timestamp
from radar_matching import looks_like_candidate_job
from radar_types import Job, is_public_http_url


class ClosedJobError(ValueError):
    """The employer detail endpoint confirms a listing is closed or out of scope."""


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def json_object(payload: bytes) -> dict[str, Any]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def compact_location(parts: Iterable[Any]) -> str:
    values = [str(value).strip() for value in parts if value]
    return "·".join(dict.fromkeys(values)) or "未披露"


def timestamp_text(value: Any) -> str:
    if value in (None, "", 0, "0"):
        return ""
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else str(value or "").strip()


def source_company(source: dict[str, Any]) -> str:
    return str(source.get("company") or source["name"]).replace("官方", "").strip()


def truthy_flag(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {
        "1", "true", "yes", "y", "on",
    }


def amazon_posted_at(value: Any) -> str:
    """Normalize the human-readable dates returned by Amazon Jobs search."""
    if value in (None, ""):
        return ""
    parsed = parse_timestamp(value)
    if parsed:
        return parsed.isoformat()
    text = str(value).strip()
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return ""


def amazon_country_codes(raw: dict[str, Any]) -> set[str]:
    values: list[Any] = [raw.get("country_code")]
    locations = raw.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, str):
                try:
                    location = json.loads(location)
                except json.JSONDecodeError:
                    continue
            if isinstance(location, dict):
                values.extend(
                    (
                        location.get("country_code"),
                        location.get("countryCode"),
                        location.get("normalizedCountryCode"),
                        location.get("countryIso3a"),
                    )
                )
    return {str(value).strip().upper() for value in values if value}


def amazon_location(raw: dict[str, Any]) -> str:
    values: list[str] = []
    locations = raw.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, str):
                try:
                    decoded = json.loads(location)
                except json.JSONDecodeError:
                    decoded = location.strip()
                location = decoded
            if isinstance(location, str) and location:
                values.append(location)
                continue
            if not isinstance(location, dict):
                continue
            normalized = str(
                location.get("normalizedLocation")
                or location.get("normalized_location")
                or location.get("locationNonStemming")
                or location.get("location")
                or ""
            ).strip()
            place = normalized or compact_location(
                (
                    location.get("country"),
                    location.get("state"),
                    location.get("region"),
                    location.get("city"),
                )
            )
            if place != "未披露":
                values.append(place)
    if values:
        return " / ".join(dict.fromkeys(value for value in values if value))
    for key in ("normalized_location", "location", "job_location", "city"):
        if raw.get(key):
            return str(raw[key]).strip()
    return "未披露"


def amazon_job_url(raw: dict[str, Any]) -> str:
    value = str(raw.get("job_path") or raw.get("url") or "").strip()
    url = urllib.parse.urljoin("https://www.amazon.jobs/", value)
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in {"amazon.jobs", "www.amazon.jobs"}
        or not re.fullmatch(
            r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?jobs/\d+(?:/[^?#]*)?",
            parsed.path,
            flags=re.IGNORECASE,
        )
    ):
        return ""
    return urllib.parse.urlunsplit(("https", parsed.netloc.casefold(), parsed.path, "", ""))


def parse_amazon(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    """Parse live mainland-China roles from Amazon's public jobs search API."""
    data = json_object(payload)
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("Amazon response has no jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").strip().casefold()
        schedule = "\n".join(
            str(raw.get(key) or "")
            for key in (
                "employment_type", "job_type", "job_schedule_type", "schedule_type",
            )
        )
        if (
            raw.get("active") is False
            or raw.get("is_active") is False
            or raw.get("can_apply") is False
            or status in {"closed", "expired", "inactive", "filled", "archived"}
            or truthy_flag(raw.get("is_intern"))
            or truthy_flag(raw.get("university_job"))
            or bool(
                re.search(
                    r"(?i)\bintern(?:ship)?\b|part[ -]?time|temporary|seasonal|contract",
                    schedule,
                )
            )
            or "CHN" not in amazon_country_codes(raw)
        ):
            continue
        job_id = str(raw.get("id_icims") or raw.get("id") or "").strip()
        url = amazon_job_url(raw)
        summary = strip_html(
            "\n".join(
                str(raw.get(key) or "")
                for key in (
                    "description",
                    "basic_qualifications",
                    "preferred_qualifications",
                )
                if raw.get(key)
            )
        )
        posted_at = amazon_posted_at(raw.get("posted_date") or raw.get("date_posted"))
        job = Job(
            title=str(raw.get("title") or "").strip(),
            url=url,
            summary=summary,
            source=str(source["name"]),
            job_id=job_id,
            location=amazon_location(raw),
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=posted_at,
            date_basis="published" if posted_at else "unknown",
            active=True,
        )
        if (
            job.title
            and job_id
            and job.url
            and len(job.summary) >= 80
            and looks_like_candidate_job(job)
        ):
            jobs.append(job)
    try:
        total = int(data.get("hits") or len(raw_jobs))
    except (TypeError, ValueError):
        total = len(raw_jobs)
    return jobs, total


def microsoft_job_url(value: Any) -> str:
    url = urllib.parse.urljoin(
        "https://apply.careers.microsoft.com/",
        str(value or "").strip(),
    )
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "apply.careers.microsoft.com"
        or not re.fullmatch(r"/careers/job/\d+", parsed.path)
    ):
        return ""
    return urllib.parse.urlunsplit(("https", parsed.netloc.casefold(), parsed.path, "", ""))


def microsoft_location(raw: dict[str, Any]) -> str:
    raw_locations = raw.get("locations")
    if isinstance(raw_locations, list):
        locations = [str(value).strip() for value in raw_locations if value]
        if locations:
            return " / ".join(dict.fromkeys(locations))
    return str(raw.get("location") or "未披露").strip() or "未披露"


def microsoft_is_mainland(raw: dict[str, Any]) -> bool:
    values: list[Any] = []
    for key in ("location", "locations", "standardizedLocations"):
        value = raw.get(key)
        values.extend(value if isinstance(value, list) else (value,))
    folded = "\n".join(str(value).casefold() for value in values if value)
    if any(region in folded for region in ("hong kong", "macao", "macau", "taiwan")) and not any(
        city in folded
        for city in ("beijing", "shanghai", "shenzhen", "guangzhou", "suzhou")
    ):
        return False
    return (
        "china," in folded
        or folded.startswith("china")
        or ", china" in folded
        or folded.endswith(" china")
        or ", cn" in folded
    )


def parse_microsoft_search(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    """Parse discovery candidates; callers must confirm every item via the detail API."""
    response = json_object(payload)
    data = response.get("data")
    if response.get("status") != 200 or not isinstance(data, dict):
        raise ValueError("Microsoft search response is not successful")
    raw_jobs = data.get("positions")
    if not isinstance(raw_jobs, list):
        raise ValueError("Microsoft search response has no positions list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict) or not microsoft_is_mainland(raw):
            continue
        job_id = str(raw.get("id") or "").strip()
        job = Job(
            title=str(raw.get("name") or "").strip(),
            url=microsoft_job_url(raw.get("positionUrl")),
            summary=strip_html(
                "\n".join(
                    str(raw.get(key) or "")
                    for key in ("department", "workLocationOption", "locationFlexibility")
                    if raw.get(key)
                )
            ),
            source=str(source["name"]),
            job_id=job_id,
            location=microsoft_location(raw),
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("postedTs") or raw.get("creationTs")),
            date_basis="published" if raw.get("postedTs") else "created" if raw.get("creationTs") else "unknown",
            active=False,
        )
        if job.title and job_id and job.url and looks_like_candidate_job(job):
            jobs.append(job)
    try:
        total = int(data.get("count") or len(raw_jobs))
    except (TypeError, ValueError):
        total = len(raw_jobs)
    return jobs, total


def microsoft_text_list(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, list):
            text = ", ".join(str(item).strip() for item in value if item)
        else:
            text = str(value or "").strip()
        if text:
            return text
    return ""


def parse_microsoft_detail(
    payload: bytes,
    source: dict[str, Any],
    expected_job_id: str = "",
) -> Job:
    response = json_object(payload)
    raw = response.get("data")
    if response.get("status") != 200 or not isinstance(raw, dict):
        raise ClosedJobError("Microsoft detail is unavailable")
    job_id = str(raw.get("id") or "").strip()
    if expected_job_id and job_id != str(expected_job_id):
        raise ValueError("Microsoft detail id does not match search result")
    user_actions = raw.get("positionUserActions")
    apply_action = user_actions.get("applyAction") if isinstance(user_actions, dict) else {}
    apply_status = str(
        apply_action.get("status") if isinstance(apply_action, dict) else ""
    ).casefold()
    employment = microsoft_text_list(raw, "efcustomTextEmploymentType", "employmentType")
    allowed_statuses = source.get("active_apply_statuses", ("log_in",))
    if isinstance(allowed_statuses, str):
        allowed_statuses = (allowed_statuses,)
    if apply_status not in {str(value).casefold() for value in allowed_statuses} or re.search(
        r"(?i)intern(?:ship)?|part[ -]?time|temp(?:orary)?|contract",
        employment,
    ):
        raise ClosedJobError("Microsoft detail confirms the listing is closed or out of scope")
    metadata = [
        ("Travel", microsoft_text_list(raw, "efcustomTextRequiredTravel", "travel")),
        ("Work site", microsoft_text_list(raw, "efcustomTextWorkSite", "workLocationOption")),
        ("Role type", microsoft_text_list(raw, "efcustomTextRoletype", "role")),
        ("Employment", employment),
        ("Profession", microsoft_text_list(raw, "efcustomTextCurrentProfession", "profession")),
    ]
    description = strip_html(str(raw.get("jobDescription") or ""))
    details = ". ".join(f"{label}: {value}" for label, value in metadata if value)
    summary = "\n".join(value for value in (description, details) if value)
    job = Job(
        title=str(raw.get("name") or "").strip(),
        url=microsoft_job_url(raw.get("publicUrl") or raw.get("positionUrl")),
        summary=summary,
        source=str(source["name"]),
        job_id=job_id,
        location=microsoft_location(raw),
        scope=str(source.get("scope", "china")),
        official=True,
        source_key=str(source.get("key", source["name"])),
        company=source_company(source),
        published_at=timestamp_text(raw.get("postedTs") or raw.get("creationTs")),
        date_basis="published" if raw.get("postedTs") else "created" if raw.get("creationTs") else "unknown",
        active=True,
    )
    if not job.title or not job_id or not job.url or len(description) < 80:
        raise ValueError("Microsoft detail is missing a substantive public job description")
    if not microsoft_is_mainland(raw) or not looks_like_candidate_job(job):
        raise ClosedJobError("Microsoft detail is outside the configured role or location scope")
    return job


def official_job_url_allowed(
    value: str,
    source: dict[str, Any],
    default_hosts: Iterable[str],
) -> bool:
    """Keep an ATS payload from assigning employer trust to an arbitrary external URL."""
    if not is_public_http_url(value):
        return False
    parsed = urllib.parse.urlsplit(value)
    configured = source.get("allowed_job_hosts") or default_hosts
    if isinstance(configured, str):
        configured = (configured,)
    allowed_hosts = {str(host).casefold() for host in configured}
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in allowed_hosts


def parse_bytedance(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    data = json_object(payload)
    result = data.get("data")
    if data.get("code") != 0 or not isinstance(result, dict):
        raise ValueError("ByteDance response is not successful")
    raw_jobs = result.get("job_post_list")
    if not isinstance(raw_jobs, list):
        raise ValueError("ByteDance response has no jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        recruit_type = raw.get("recruit_type") if isinstance(raw.get("recruit_type"), dict) else {}
        parent = recruit_type.get("parent") if isinstance(recruit_type.get("parent"), dict) else {}
        if parent and str(parent.get("id") or "") != "1":
            continue
        job_id = str(raw.get("id") or raw.get("code") or "")
        city_info = raw.get("city_info") if isinstance(raw.get("city_info"), dict) else {}
        raw_cities = raw.get("city_list") if isinstance(raw.get("city_list"), list) else []
        nested = raw.get("job_post_info") if isinstance(raw.get("job_post_info"), dict) else {}
        location = str(city_info.get("name") or "").strip() or " / ".join(
            dict.fromkeys(
                str(city.get("name") or "").strip()
                for city in raw_cities
                if isinstance(city, dict) and city.get("name")
            )
        ) or "未披露"
        job = Job(
            title=str(raw.get("title") or "").strip(),
            url=f"https://jobs.bytedance.com/experienced/position/{urllib.parse.quote(job_id)}/detail",
            summary=strip_html(
                "\n".join(str(raw.get(key) or "") for key in ("description", "requirement"))
            ),
            source=str(source["name"]),
            job_id=job_id,
            location=location,
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("publish_time")),
            date_basis="published" if raw.get("publish_time") else "unknown",
            active=True,
            valid_through=timestamp_text(nested.get("expiry_time")),
        )
        if job.title and job_id and looks_like_candidate_job(job):
            jobs.append(job)
    return jobs, int(result.get("count") or len(raw_jobs))


def parse_alibaba(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    data = json_object(payload)
    content = data.get("content")
    if data.get("success") is not True or not isinstance(content, dict):
        raise ValueError("Alibaba response is not successful")
    raw_jobs = content.get("datas")
    if not isinstance(raw_jobs, list):
        raise ValueError("Alibaba response has no jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        job_id = str(raw.get("id") or raw.get("code") or "")
        position_url = urllib.parse.urljoin(
            "https://talent.alibaba.com/",
            str(raw.get("positionUrl") or ""),
        )
        parsed_position_url = urllib.parse.urlsplit(position_url)
        if (
            parsed_position_url.scheme != "https"
            or (parsed_position_url.hostname or "").casefold() != "talent.alibaba.com"
            or parsed_position_url.path != "/off-campus/position-detail"
        ):
            continue
        experience = raw.get("experience") if isinstance(raw.get("experience"), dict) else {}
        experience_text = ""
        if experience.get("from") is not None:
            experience_text = f"Experience: {experience['from']}+ years"
        published = raw.get("publishTime")
        modified = raw.get("modifyTime")
        locations = raw.get("workLocations") if isinstance(raw.get("workLocations"), list) else []
        job = Job(
            title=str(raw.get("name") or "").strip(),
            url=position_url,
            summary=strip_html(
                "\n".join(
                    value
                    for value in (
                        str(raw.get("description") or ""),
                        str(raw.get("requirement") or ""),
                        experience_text,
                    )
                    if value
                )
            ),
            source=str(source["name"]),
            job_id=job_id,
            location=" / ".join(str(value).strip() for value in locations if value) or "未披露",
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(published or modified),
            date_basis="published" if published else "updated" if modified else "unknown",
            active=True,
        )
        if job.title and job_id and is_public_http_url(job.url) and looks_like_candidate_job(job):
            jobs.append(job)
    return jobs, int(content.get("totalCount") or len(raw_jobs))


def parse_greenhouse(payload: bytes, source: dict[str, Any]) -> list[Job]:
    raw_jobs = json_object(payload).get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("Greenhouse response has no jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            continue
        location = raw.get("location") or {}
        job = Job(
            title=str(raw.get("title") or "").strip(),
            url=str(raw.get("absolute_url") or "").strip(),
            summary=strip_html(str(raw.get("content") or "")),
            source=str(source["name"]),
            job_id=str(raw.get("id") or ""),
            location=str(location.get("name") or "未披露") if isinstance(location, dict) else "未披露",
            scope=str(source.get("scope", "global")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("updated_at")),
            date_basis="updated" if raw.get("updated_at") else "unknown",
            active=True,
        )
        if (
            job.title
            and official_job_url_allowed(
                job.url,
                source,
                ("job-boards.greenhouse.io", "boards.greenhouse.io"),
            )
            and looks_like_candidate_job(job)
        ):
            jobs.append(job)
    return jobs


def ashby_compensation(raw: dict[str, Any]) -> str:
    compensation = raw.get("compensation")
    if not isinstance(compensation, dict):
        return ""
    for key in ("scrapeableCompensationSalarySummary", "compensationTierSummary"):
        if compensation.get(key):
            return strip_html(str(compensation[key]))
    return ""


def parse_ashby(payload: bytes, source: dict[str, Any]) -> list[Job]:
    raw_jobs = json_object(payload).get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("Ashby response has no jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict) or raw.get("isListed") is False:
            continue
        description = str(raw.get("descriptionPlain") or strip_html(str(raw.get("descriptionHtml") or "")))
        compensation = ashby_compensation(raw)
        if compensation:
            description = f"{description}\nCompensation: {compensation}"
        job = Job(
            title=str(raw.get("title") or "").strip(),
            url=str(raw.get("jobUrl") or "").strip(),
            summary=description,
            source=str(source["name"]),
            job_id=str(raw.get("id") or raw.get("jobUrl") or ""),
            location=str(raw.get("location") or "未披露"),
            scope=str(source.get("scope", "global")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("publishedAt") or raw.get("published_at")),
            date_basis="published" if raw.get("publishedAt") or raw.get("published_at") else "unknown",
            active=True,
            valid_through=timestamp_text(raw.get("validThrough") or raw.get("applicationDeadline")),
        )
        if (
            job.title
            and official_job_url_allowed(job.url, source, ("jobs.ashbyhq.com",))
            and looks_like_candidate_job(job)
        ):
            jobs.append(job)
    return jobs


def lever_salary(raw: dict[str, Any]) -> str:
    salary = raw.get("salaryRange")
    if not isinstance(salary, dict) or salary.get("min") is None or salary.get("max") is None:
        return ""
    low, high = float(salary["min"]), float(salary["max"])
    if high > 1_000_000:  # Some Lever boards expose minor currency units.
        low, high = low / 100, high / 100
    currency = str(salary.get("currency") or "").upper()
    interval = str(salary.get("interval") or "annual")
    if currency == "USD" and high >= 10_000:
        return f"Compensation: ${low / 1000:g}K - ${high / 1000:g}K {interval}"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"Compensation: {symbol}{low:g} - {symbol}{high:g} {interval}"


def parse_lever(payload: bytes, source: dict[str, Any]) -> list[Job]:
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")
    jobs: list[Job] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        categories = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
        description = "\n".join(
            str(value)
            for value in (raw.get("descriptionPlain"), raw.get("additionalPlain"), lever_salary(raw))
            if value
        )
        job = Job(
            title=str(raw.get("text") or "").strip(),
            url=str(raw.get("hostedUrl") or "").strip(),
            summary=strip_html(description),
            source=str(source["name"]),
            job_id=str(raw.get("id") or ""),
            location=str(categories.get("location") or "未披露"),
            scope=str(source.get("scope", "global")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("createdAt")),
            date_basis="created" if raw.get("createdAt") else "unknown",
            active=True,
        )
        if (
            job.title
            and official_job_url_allowed(job.url, source, ("jobs.lever.co",))
            and looks_like_candidate_job(job)
        ):
            jobs.append(job)
    return jobs


def moka_location(raw_locations: Any) -> str:
    if not isinstance(raw_locations, list):
        return "未披露"
    places: list[str] = []
    for raw in raw_locations:
        if not isinstance(raw, dict):
            continue
        place = compact_location((raw.get("country"), raw.get("province"), raw.get("city"), raw.get("area")))
        if place != "未披露":
            places.append(place)
    return " / ".join(dict.fromkeys(places)) or "未披露"


def parse_moka(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    data = json_object(payload)
    raw_jobs = data.get("jobs")
    if data.get("code") not in (None, 0) or not isinstance(raw_jobs, list):
        raise ValueError("Moka response is not a successful jobs list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict) or raw.get("status") not in (None, "open"):
            continue
        job_id = str(raw.get("id") or "")
        published = raw.get("publishedAt") or raw.get("openedAt")
        updated = raw.get("updatedAt")
        job = Job(
            title=str(raw.get("title") or "").strip(),
            url=str(source["job_url_template"]).format(id=job_id),
            summary=strip_html(str(raw.get("description") or "")),
            source=str(source["name"]),
            job_id=job_id,
            location=moka_location(raw.get("locations")),
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(published or updated),
            date_basis="published" if published else "updated" if updated else "unknown",
            active=True,
            valid_through=timestamp_text(raw.get("closedAt")),
        )
        if job.title and job_id and looks_like_candidate_job(job):
            jobs.append(job)
    return jobs, int(data.get("total") or len(raw_jobs))


def meituan_location(raw_locations: Any) -> str:
    if not isinstance(raw_locations, list):
        return "未披露"
    return " / ".join(
        dict.fromkeys(
            str(raw.get("name") or "").strip()
            for raw in raw_locations
            if isinstance(raw, dict) and raw.get("name")
        )
    ) or "未披露"


def meituan_job(raw: dict[str, Any], source: dict[str, Any]) -> Job | None:
    if str(raw.get("jobStatus") or "") != "000" or str(raw.get("jobType") or "3") != "3":
        return None
    job_id = str(raw.get("jobUnionId") or "")
    first_post = raw.get("firstPostTime")
    job = Job(
        title=str(raw.get("name") or "").strip(),
        url=(
            "https://zhaopin.meituan.com/web/position/detail"
            f"?jobUnionId={urllib.parse.quote(job_id)}&jobShareType=1&highlightType=social"
        ),
        summary=strip_html(
            "\n".join(
                str(raw.get(key) or "")
                for key in ("jobDuty", "jobRequirement", "highLight", "workYear")
            )
        ),
        source=str(source["name"]),
        job_id=job_id,
        location=meituan_location(raw.get("cityList")),
        scope=str(source.get("scope", "china")),
        official=True,
        source_key=str(source.get("key", source["name"])),
        company=source_company(source),
        published_at=timestamp_text(first_post),
        date_basis="published" if first_post else "unknown",
        active=True,
        valid_through=timestamp_text(raw.get("expiredTime")),
    )
    return job if job.title and job_id and looks_like_candidate_job(job) else None


def parse_meituan(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    data = json_object(payload)
    result = data.get("data")
    if data.get("status") != 1 or not isinstance(result, dict):
        raise ValueError("Meituan response is not successful")
    raw_jobs = result.get("list")
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    if not isinstance(raw_jobs, list):
        raise ValueError("Meituan response has no jobs list")
    jobs = [
        job
        for raw in raw_jobs
        if isinstance(raw, dict) and (job := meituan_job(raw, source)) is not None
    ]
    return jobs, int(page.get("totalCount") or len(raw_jobs))


def parse_meituan_detail(payload: bytes, source: dict[str, Any]) -> Job:
    data = json_object(payload)
    raw = data.get("data")
    if data.get("status") != 1 or not isinstance(raw, dict):
        raise ValueError("Meituan detail response is not successful")
    job = meituan_job(raw, source)
    if job is None:
        raise ClosedJobError("Meituan detail confirms the listing is closed or out of scope")
    return job


def parse_tencent(payload: bytes, source: dict[str, Any]) -> tuple[list[Job], int]:
    data = json_object(payload)
    result = data.get("Data")
    if data.get("Code") != 200 or not isinstance(result, dict):
        raise ValueError("Tencent response is not successful")
    raw_jobs = result.get("Posts")
    if not isinstance(raw_jobs, list):
        raise ValueError("Tencent response has no Posts list")
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict) or raw.get("IsValid") is False:
            continue
        job_id = str(raw.get("PostId") or "")
        url = urllib.parse.urljoin(
            "https://careers.tencent.com/",
            str(raw.get("PostURL") or f"jobdesc.html?postId={job_id}"),
        )
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.hostname == "careers.tencent.com" and parsed_url.scheme == "http":
            url = urllib.parse.urlunsplit(
                ("https", parsed_url.netloc, parsed_url.path, parsed_url.query, parsed_url.fragment)
            )
            parsed_url = urllib.parse.urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or (parsed_url.hostname or "").casefold() != "careers.tencent.com"
            or not parsed_url.path.endswith("/jobdesc.html")
        ):
            continue
        job = Job(
            title=str(raw.get("RecruitPostName") or "").strip(),
            url=url,
            summary=strip_html(
                "\n".join(str(raw.get(key) or "") for key in ("Responsibility", "Requirement"))
            ),
            source=str(source["name"]),
            job_id=job_id,
            location=str(raw.get("LocationName") or "未披露"),
            scope=str(source.get("scope", "china")),
            official=True,
            source_key=str(source.get("key", source["name"])),
            company=source_company(source),
            published_at=timestamp_text(raw.get("LastUpdateTime")),
            date_basis="updated" if raw.get("LastUpdateTime") else "unknown",
            active=True,
        )
        if job.title and job_id and is_public_http_url(job.url) and looks_like_candidate_job(job):
            jobs.append(job)
    return jobs, int(result.get("Count") or len(raw_jobs))
