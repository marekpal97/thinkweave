"""Tests for Messenger self-chat link importer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from thinkweave.acquisition.importers.messenger import (
    Message,
    ResolvedURL,
    _build_queue_body,
    _clean_extracted_url,
    _extract_post_text,
    _extract_urls_from_text,
    _is_facebook_url,
    _title_from_url,
    classify_url,
    import_messenger,
    parse_messages,
    strip_tracking_params,
)


# ── Fixtures ───────────────────────────────────────────────────────


def _make_export(*urls: str, sender: str = "Marek Paluch") -> dict:
    """Build a minimal Messenger export JSON from a list of URLs."""
    messages = []
    for i, url in enumerate(urls):
        messages.append({
            "isUnsent": False,
            "media": [],
            "reactions": [],
            "senderName": sender,
            "text": url,
            "timestamp": 1700000000000 + i * 86400000,  # 1 day apart
            "type": "Generic",
        })
    return {
        "participants": [sender, sender],
        "threadName": f"{sender}_93",
        "messages": messages,
    }


def _write_export(tmp_path: Path, data: dict) -> Path:
    """Write export data to a JSON file and return the path."""
    path = tmp_path / "export.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── parse_messages ─────────────────────────────────────────────────


class TestParseMessages:
    def test_basic_extraction(self, tmp_path):
        data = _make_export(
            "https://arxiv.org/abs/2301.12345",
            "https://www.facebook.com/share/abc123/",
            "https://github.com/user/repo",
        )
        path = _write_export(tmp_path, data)
        msgs = parse_messages(path)

        assert len(msgs) == 3
        assert msgs[0].url == "https://arxiv.org/abs/2301.12345"
        assert msgs[1].url == "https://www.facebook.com/share/abc123/"
        assert msgs[2].url == "https://github.com/user/repo"

    def test_sorted_oldest_first(self, tmp_path):
        data = _make_export("https://a.com", "https://b.com")
        path = _write_export(tmp_path, data)
        msgs = parse_messages(path)

        assert msgs[0].timestamp < msgs[1].timestamp

    def test_skips_empty_messages(self, tmp_path):
        data = _make_export("https://arxiv.org/abs/123")
        data["messages"].append({
            "isUnsent": False,
            "media": [],
            "reactions": [],
            "senderName": "Marek Paluch",
            "text": "",
            "timestamp": 1700100000000,
            "type": "Generic",
        })
        path = _write_export(tmp_path, data)
        msgs = parse_messages(path)

        assert len(msgs) == 1

    def test_skips_messages_without_urls(self, tmp_path):
        data = _make_export("https://arxiv.org/abs/123")
        data["messages"].append({
            "isUnsent": False,
            "media": [],
            "reactions": [],
            "senderName": "Marek Paluch",
            "text": "just some text no link",
            "timestamp": 1700100000000,
            "type": "Generic",
        })
        path = _write_export(tmp_path, data)
        msgs = parse_messages(path)

        assert len(msgs) == 1


# ── strip_tracking_params ──────────────────────────────────────────


class TestStripTrackingParams:
    def test_fbclid(self):
        url = "https://arxiv.org/abs/2301.12345?fbclid=IwAR123abc"
        assert strip_tracking_params(url) == "https://arxiv.org/abs/2301.12345"

    def test_mibextid(self):
        url = "https://www.facebook.com/share/abc/?mibextid=wwXIfr"
        assert strip_tracking_params(url) == "https://www.facebook.com/share/abc"

    def test_utm_params(self):
        url = "https://example.com/article?utm_source=facebook&utm_campaign=test&good=1"
        result = strip_tracking_params(url)
        assert "utm_source" not in result
        assert "utm_campaign" not in result
        assert "good=1" in result

    def test_multiple_tracking_params(self):
        url = "https://github.com/user/repo?fbclid=abc&tab=readme-ov-file"
        result = strip_tracking_params(url)
        assert "fbclid" not in result
        assert "tab=readme-ov-file" in result

    def test_preserves_meaningful_params(self):
        url = "https://arxiv.org/abs/2301.12345?v=2"
        assert "v=2" in strip_tracking_params(url)

    def test_no_params(self):
        url = "https://arxiv.org/abs/2301.12345"
        assert strip_tracking_params(url) == "https://arxiv.org/abs/2301.12345"


# ── classify_url ───────────────────────────────────────────────────


class TestClassifyUrl:
    def test_facebook_share(self):
        assert classify_url("https://www.facebook.com/share/abc/") == "facebook"

    def test_facebook_m(self):
        assert classify_url("https://m.facebook.com/story.php?id=123") == "facebook"

    def test_arxiv(self):
        assert classify_url("https://arxiv.org/abs/2301.12345") == "direct"

    def test_github(self):
        assert classify_url("https://github.com/user/repo") == "direct"

    def test_noise_instagram(self):
        assert classify_url("https://www.instagram.com/") == "noise"

    def test_l_facebook(self):
        assert classify_url("https://l.facebook.com/l.php?u=https%3A//arxiv.org") == "facebook"


# ── _is_facebook_url ──────────────────────────────────────────────


class TestIsFacebookUrl:
    def test_www(self):
        assert _is_facebook_url("https://www.facebook.com/share/abc/")

    def test_mobile(self):
        assert _is_facebook_url("https://m.facebook.com/story.php")

    def test_l_redirect(self):
        assert _is_facebook_url("https://l.facebook.com/l.php?u=xyz")

    def test_not_facebook(self):
        assert not _is_facebook_url("https://arxiv.org/abs/123")

    def test_fb_com(self):
        assert _is_facebook_url("https://fb.com/something")


# ── _clean_extracted_url ───────────────────────────────────────────


class TestCleanExtractedUrl:
    def test_markdown_artifact(self):
        url = "https://arxiv.org/abs/2601.12538](https://arxiv.org/abs/2601.12538"
        result = _clean_extracted_url(url)
        assert result == "https://arxiv.org/abs/2601.12538"

    def test_trailing_punctuation(self):
        url = "https://arxiv.org/abs/2301.12345."
        result = _clean_extracted_url(url)
        assert result == "https://arxiv.org/abs/2301.12345"

    def test_strips_tracking(self):
        url = "https://arxiv.org/abs/123?fbclid=IwAR123"
        result = _clean_extracted_url(url)
        assert "fbclid" not in result


# ── _extract_post_text ─────────────────────────────────────────────


class TestExtractPostText:
    def test_message_text_json(self):
        html = '''<script>"message":{"text":"Check this paper: https:\\/\\/arxiv.org\\/abs\\/123"}</script>'''
        result = _extract_post_text(html)
        assert "arxiv.org/abs/123" in result

    def test_meta_description_fallback(self):
        html = '<meta name="description" content="A great paper about LLMs">'
        result = _extract_post_text(html)
        assert "great paper about LLMs" in result

    def test_prefers_message_text_over_description(self):
        html = (
            '<meta name="description" content="short desc">'
            '"message":{"text":"full post text with details"}'
        )
        result = _extract_post_text(html)
        assert "full post text with details" in result

    def test_empty_html(self):
        assert _extract_post_text("<html><body></body></html>") == ""


# ── _extract_urls_from_text ────────────────────────────────────────


class TestExtractUrlsFromText:
    def test_protocol_url(self):
        text = "Check this: https://arxiv.org/abs/2301.12345"
        urls = _extract_urls_from_text(text)
        assert any("arxiv.org/abs/2301.12345" in u for u in urls)

    def test_bare_domain(self):
        text = "See arxiv.org/abs/2301.12345 for the paper"
        urls = _extract_urls_from_text(text)
        assert any("arxiv.org/abs/2301.12345" in u for u in urls)

    def test_filters_facebook(self):
        text = "https://facebook.com/post/123 and https://arxiv.org/abs/456"
        urls = _extract_urls_from_text(text)
        assert len(urls) == 1
        assert "arxiv.org" in urls[0]

    def test_filters_noise(self):
        text = "https://support.google.com/chrome/answer/123 and https://arxiv.org/abs/456"
        urls = _extract_urls_from_text(text)
        assert len(urls) == 1
        assert "arxiv.org" in urls[0]

    def test_github_bare(self):
        text = "Code at github.com/user/repo"
        urls = _extract_urls_from_text(text)
        assert any("github.com/user/repo" in u for u in urls)

    def test_markdown_link_cleaned(self):
        text = "https://arxiv.org/abs/123](https://arxiv.org/abs/123)"
        urls = _extract_urls_from_text(text)
        assert urls[0] == "https://arxiv.org/abs/123"


# ── _title_from_url ───────────────────────────────────────────────


class TestTitleFromUrl:
    def test_arxiv(self):
        title = _title_from_url("https://arxiv.org/abs/2301.12345")
        assert title == "arxiv 2301.12345"

    def test_github(self):
        title = _title_from_url("https://github.com/user/awesome-repo")
        assert title == "GitHub: user/awesome-repo"

    def test_with_description(self):
        title = _title_from_url("https://example.com/article", "A great article about ML")
        assert title == "A great article about ML"

    def test_fallback_domain(self):
        title = _title_from_url("https://unknown-site.com/page")
        assert "unknown-site.com" in title


# ── import_messenger (integration) ─────────────────────────────────


class TestImportMessenger:
    def test_dry_run(self, tmp_path):
        data = _make_export(
            "https://arxiv.org/abs/2301.12345",
            "https://www.facebook.com/share/abc/?mibextid=wwXIfr",
            "https://github.com/user/repo?fbclid=IwAR123",
        )
        path = _write_export(tmp_path, data)

        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")

        stats = import_messenger(cfg, json_path=path, dry_run=True)
        assert stats["dry_run"] is True
        assert stats["direct"] == 2  # arxiv + github
        assert stats["facebook"] == 1

    def test_no_resolve_queues_direct_only(self, tmp_path):
        data = _make_export(
            "https://arxiv.org/abs/2301.12345",
            "https://www.facebook.com/share/abc/",
            "https://github.com/user/repo",
        )
        path = _write_export(tmp_path, data)

        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        (tmp_path / "vault").mkdir()

        stats = import_messenger(cfg, json_path=path, resolve=False)
        assert stats["queued"] == 2  # arxiv + github only
        assert stats["queued_resolved"] == 2

    def test_idempotency(self, tmp_path):
        data = _make_export("https://arxiv.org/abs/2301.12345")
        path = _write_export(tmp_path, data)

        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        (tmp_path / "vault").mkdir()

        stats1 = import_messenger(cfg, json_path=path, resolve=False)
        assert stats1["queued"] == 1

        stats2 = import_messenger(cfg, json_path=path, resolve=False)
        assert stats2["queued"] == 0
        assert stats2["skipped"] == 1

    def test_file_not_found(self, tmp_path):
        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        stats = import_messenger(cfg, json_path=tmp_path / "nonexistent.json")
        assert "error" in stats

    def test_date_filter(self, tmp_path):
        data = _make_export(
            "https://arxiv.org/abs/1111.1111",
            "https://arxiv.org/abs/2222.2222",
            "https://arxiv.org/abs/3333.3333",
        )
        # Messages are 1 day apart starting from 2023-11-14
        path = _write_export(tmp_path, data)

        from thinkweave.core.config import Config

        cfg = Config(vault_root=tmp_path / "vault")
        (tmp_path / "vault").mkdir()

        # Only import the last message
        stats = import_messenger(
            cfg, json_path=path, resolve=False, since="2023-11-16"
        )
        assert stats["queued"] == 1
