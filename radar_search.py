"""Low-volume public search helpers shared by job and trend discovery."""

from __future__ import annotations

import html
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

from radar_types import is_public_http_url


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    summary: str


class DuckDuckGoLiteParser(HTMLParser):
    """Extract result links and snippets from DuckDuckGo's public Lite page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title_parts: list[str] | None = None
        self._snippet_parts: list[str] | None = None
        self._href = ""

    @staticmethod
    def _has_class(attrs: list[tuple[str, str | None]], class_name: str) -> bool:
        classes = dict(attrs).get("class") or ""
        return class_name in classes.split()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and self._has_class(attrs, "result-link"):
            self._title_parts = []
            self._href = dict(attrs).get("href") or ""
        elif tag == "td" and self._has_class(attrs, "result-snippet"):
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._title_parts is not None:
            title = " ".join("".join(self._title_parts).split())
            if title and self._href:
                self.results.append({"title": title, "href": self._href, "summary": ""})
            self._title_parts = None
            self._href = ""
        elif tag == "td" and self._snippet_parts is not None:
            summary = " ".join("".join(self._snippet_parts).split())
            for result in reversed(self.results):
                if not result["summary"]:
                    result["summary"] = summary
                    break
            self._snippet_parts = None


def duckduckgo_lite_url(query: str, language: str) -> str:
    locale = "cn-zh" if language.casefold().startswith("zh") else "us-en"
    return "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode(
        {"q": query, "kl": locale}
    )


def decode_duckduckgo_url(value: str) -> str:
    value = html.unescape(value.strip())
    if value.startswith("//"):
        value = f"https:{value}"
    if not is_public_http_url(value):
        return ""
    parsed = urllib.parse.urlsplit(value)
    if (parsed.hostname or "").casefold() in {"duckduckgo.com", "www.duckduckgo.com"}:
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        return target if is_public_http_url(target) else ""
    return value


def parse_duckduckgo_results(payload: bytes, limit: int) -> list[SearchResult]:
    parser = DuckDuckGoLiteParser()
    parser.feed(payload.decode("utf-8", errors="ignore"))
    results: list[SearchResult] = []
    for raw in parser.results:
        url = decode_duckduckgo_url(raw["href"])
        if url:
            results.append(SearchResult(raw["title"], url, raw["summary"]))
        if len(results) >= limit:
            break
    return results
