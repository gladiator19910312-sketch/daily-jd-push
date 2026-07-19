"""Pure parsers for official ATS payloads."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable

from radar_matching import looks_like_product_job
from radar_types import Job, is_public_http_url


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
        )
        if job.title and is_public_http_url(job.url) and looks_like_product_job(job):
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
        )
        if job.title and is_public_http_url(job.url) and looks_like_product_job(job):
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
        )
        if job.title and is_public_http_url(job.url) and looks_like_product_job(job):
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
        )
        if job.title and job_id and looks_like_product_job(job):
            jobs.append(job)
    return jobs, int(data.get("total") or len(raw_jobs))
