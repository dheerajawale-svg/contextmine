"""Tests for web_sync module: crawling helpers and pure functions."""

import json
from unittest.mock import MagicMock, patch

from contextmine_worker.web_sync import (
    WebPage,
    WebSyncStats,
    _crawl_spider_md,
    _extract_links_from_html,
    _is_excluded,
    extract_markdown_with_trafilatura,
    get_page_title,
    is_url_in_scope,
    run_spider_md,
)

# ---------------------------------------------------------------------------
# is_url_in_scope
# ---------------------------------------------------------------------------


class TestIsUrlInScope:
    def test_same_host_same_path(self) -> None:
        assert is_url_in_scope(
            "https://docs.example.com/guide/start", "https://docs.example.com/guide"
        )

    def test_different_host(self) -> None:
        assert not is_url_in_scope("https://other.com/page", "https://example.com/")

    def test_different_scheme(self) -> None:
        assert not is_url_in_scope("http://example.com/page", "https://example.com/")

    def test_root_base_allows_all_paths(self) -> None:
        assert is_url_in_scope("https://example.com/anything/deep", "https://example.com/")

    def test_empty_base_path(self) -> None:
        assert is_url_in_scope("https://example.com/anything", "https://example.com")

    def test_deeper_path_out_of_scope(self) -> None:
        assert not is_url_in_scope("https://example.com/other", "https://example.com/docs")

    def test_exact_path_match(self) -> None:
        assert is_url_in_scope("https://example.com/docs", "https://example.com/docs")


# ---------------------------------------------------------------------------
# get_page_title
# ---------------------------------------------------------------------------


class TestGetPageTitle:
    def test_returns_title_when_set(self) -> None:
        page = WebPage(url="https://example.com", title="Hello", markdown="md", content_hash="abc")
        assert get_page_title(page) == "Hello"

    def test_falls_back_to_url_path(self) -> None:
        page = WebPage(
            url="https://example.com/docs/intro", title="", markdown="md", content_hash="abc"
        )
        assert get_page_title(page) == "/docs/intro"

    def test_falls_back_to_full_url(self) -> None:
        page = WebPage(url="https://example.com", title="", markdown="md", content_hash="abc")
        title = get_page_title(page)
        assert title  # Either "/" or the full URL


# ---------------------------------------------------------------------------
# extract_markdown_with_trafilatura
# ---------------------------------------------------------------------------


class TestExtractMarkdown:
    def test_empty_html_returns_none(self) -> None:
        assert extract_markdown_with_trafilatura("") is None
        assert extract_markdown_with_trafilatura(None) is None

    @patch("contextmine_worker.web_sync.trafilatura.extract", return_value="Extracted text content")
    def test_successful_extraction(self, mock_extract: MagicMock) -> None:
        result = extract_markdown_with_trafilatura("<html><body>content</body></html>")
        assert result == "Extracted text content"
        mock_extract.assert_called_once()

    @patch(
        "contextmine_worker.web_sync.trafilatura.extract", side_effect=RuntimeError("parse error")
    )
    def test_extraction_error_returns_none(self, mock_extract: MagicMock) -> None:
        result = extract_markdown_with_trafilatura("<html><body>content</body></html>")
        assert result is None


# ---------------------------------------------------------------------------
# WebSyncStats / WebPage dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_web_sync_stats_defaults(self) -> None:
        stats = WebSyncStats()
        assert stats.pages_crawled == 0
        assert stats.errors == []

    def test_web_page_optional_fields(self) -> None:
        page = WebPage(url="u", title="t", markdown="m", content_hash="h")
        assert page.etag is None
        assert page.last_modified is None


# ---------------------------------------------------------------------------
# _crawl_spider_md (subprocess path)
# ---------------------------------------------------------------------------


class TestCrawlSpiderMd:
    @patch("contextmine_worker.web_sync.subprocess.run", side_effect=FileNotFoundError)
    def test_binary_not_found_returns_empty(self, mock_run: MagicMock) -> None:
        pages = _crawl_spider_md("https://example.com")
        assert pages == []

    @patch("contextmine_worker.web_sync.subprocess.run")
    @patch("contextmine_worker.web_sync.extract_markdown_with_trafilatura")
    def test_parses_jsonl_output(self, mock_extract: MagicMock, mock_run: MagicMock) -> None:
        md_content = "A" * 60  # > 50 chars threshold
        mock_extract.return_value = md_content

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"url": "https://example.com/p1", "html": "<p>hello</p>", "title": "Page 1"}
            )
            + "\n",
        )

        pages = _crawl_spider_md("https://example.com", max_pages=5)
        assert len(pages) == 1
        assert pages[0].url == "https://example.com/p1"
        assert pages[0].title == "Page 1"

    @patch("contextmine_worker.web_sync.subprocess.run")
    def test_nonzero_returncode_returns_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        pages = _crawl_spider_md("https://example.com")
        assert pages == []


# ---------------------------------------------------------------------------
# run_spider_md (dispatch logic)
# ---------------------------------------------------------------------------


class TestRunSpiderMd:
    @patch("contextmine_worker.web_sync._crawl_python")
    @patch("contextmine_worker.web_sync._crawl_spider_md")
    def test_falls_back_to_python_crawler(
        self, mock_spider: MagicMock, mock_python: MagicMock
    ) -> None:
        mock_spider.return_value = []
        mock_python.return_value = [
            WebPage(url="https://example.com", title="T", markdown="md", content_hash="h")
        ]

        pages = run_spider_md("https://example.com")
        assert len(pages) == 1
        mock_python.assert_called_once()

    @patch("contextmine_worker.web_sync._crawl_spider_md")
    def test_returns_spider_pages_when_available(self, mock_spider: MagicMock) -> None:
        mock_spider.return_value = [
            WebPage(url="https://example.com", title="T", markdown="md", content_hash="h")
        ]
        pages = run_spider_md("https://example.com")
        assert len(pages) == 1


# ---------------------------------------------------------------------------
# exclude_patterns (noise URL filtering)
# ---------------------------------------------------------------------------


class TestIsExcluded:
    def test_no_patterns_never_excludes(self) -> None:
        assert not _is_excluded("https://example.com/related/click?x=1", None)
        assert not _is_excluded("https://example.com/related/click?x=1", [])

    def test_substring_match(self) -> None:
        assert _is_excluded(
            "https://docs.example.com/hc/en-us/related/click?data=abc", ["/related/click"]
        )
        assert _is_excluded("https://docs.example.com/hc/en-us/search?utf8=1", ["/search?"])
        assert not _is_excluded("https://docs.example.com/hc/en-us/articles/123", ["/search?"])

    def test_case_insensitive(self) -> None:
        assert _is_excluded("https://example.com/Related/Click?x=1", ["/related/click"])


class TestExcludePatternsInCrawlers:
    @patch("contextmine_worker.web_sync.subprocess.run")
    @patch("contextmine_worker.web_sync.extract_markdown_with_trafilatura")
    def test_spider_md_output_filters_excluded_urls(
        self, mock_extract: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_extract.return_value = "A" * 60
        lines = [
            {"url": "https://example.com/articles/1", "html": "<p>a</p>", "title": "A"},
            {
                "url": "https://example.com/related/click?data=x",
                "html": "<p>a</p>",
                "title": "Dupe",
            },
        ]
        mock_run.return_value = MagicMock(
            returncode=0, stdout="\n".join(json.dumps(line) for line in lines) + "\n"
        )

        pages = _crawl_spider_md("https://example.com", exclude_patterns=["/related/click"])
        assert [p.url for p in pages] == ["https://example.com/articles/1"]

    def test_extract_links_skips_excluded(self) -> None:
        from collections import deque

        visited: set[str] = set()
        queue: deque[str] = deque()
        html = (
            '<html><body><a href="/articles/1">a</a>'
            '<a href="/related/click?data=x">dupe</a></body></html>'
        )
        _extract_links_from_html(
            html, "https://example.com/", "https://example.com/", visited, queue, ["/related/click"]
        )
        assert list(queue) == ["https://example.com/articles/1"]

    @patch("contextmine_worker.web_sync._crawl_spider_md")
    def test_run_spider_md_passes_exclude_patterns(self, mock_spider: MagicMock) -> None:
        mock_spider.return_value = [
            WebPage(url="https://example.com", title="T", markdown="md", content_hash="h")
        ]
        run_spider_md("https://example.com", exclude_patterns=["/search?"])
        assert mock_spider.call_args.kwargs["exclude_patterns"] == ["/search?"]
