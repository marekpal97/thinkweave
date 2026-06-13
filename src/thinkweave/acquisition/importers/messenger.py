"""Import links from Facebook Messenger self-conversation export.

Parses the Messenger JSON export, extracts URLs from self-sent messages,
resolves Facebook wrapper URLs by fetching post content, and creates
research queue items (todo+research tagged notes) for processing via
`/research --queue`.

Usage:
    weave import messenger ~/path/to/export.json [--dry-run] [--no-resolve]
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from thinkweave.core.config import Config, load_config
from thinkweave.core.indexer import Indexer
from thinkweave.core.schemas import NoteType
from thinkweave.core.vault import VaultManager

_MANIFEST_NAME = "messenger_import.json"

# Tracking params to strip from URLs.
_TRACKING_PARAMS = {
    "fbclid",
    "mibextid",
    "rdid",
    "share_url",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_id",
    "lid",
    "si",
}

# Domains to filter out (noise, not research content).
_NOISE_DOMAINS = {
    "support.google.com",
    "play.google.com",
    "apps.apple.com",
    "instagram.com",
    "www.instagram.com",
    "twitter.com",
    "x.com",
}

# Known URL shortener domains — need redirect-following.
_SHORTENER_DOMAINS = {
    "shorturl.at",
    "hubs.la",
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
    "buff.ly",
}

# Bare domain patterns for URLs without protocol prefix.
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|[\s\-/])("
    r"arxiv\.org|github\.com|huggingface\.co|openai\.com|"
    r"paperswithcode\.com|docs\.google\.com|kaggle\.com|"
    r"medium\.com|youtube\.com|youtu\.be"
    r")"
    r"(/[^\s\"'<>\\,)]*)",
)

# Primary URL regex.
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")

# Facebook domain check.
_FB_DOMAINS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.com"}

_SSL_CTX = ssl.create_default_context()


# ── Data model ─────────────────────────────────────────────────────


@dataclass
class Message:
    """A single self-sent Messenger message containing a URL."""

    url: str
    timestamp: datetime
    raw_text: str


@dataclass
class ResolvedURL:
    """Result of resolving a URL (possibly through Facebook)."""

    url: str  # The final resolved URL (or original if direct)
    original_url: str  # The URL from the Messenger message
    description: str  # Post text / context snippet
    category: str  # "direct", "resolved", "description_only", "empty", "error"
    timestamp: datetime


# ── Parsing ────────────────────────────────────────────────────────


def parse_messages(json_path: Path) -> list[Message]:
    """Parse Messenger export JSON and extract messages with URLs."""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    messages: list[Message] = []
    for m in data.get("messages", []):
        text = m.get("text", "") or ""
        if not text.strip():
            continue

        # Extract URL from message text
        url_match = _URL_RE.search(text)
        if not url_match:
            continue

        ts_ms = m.get("timestamp", 0)
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        messages.append(Message(url=url_match.group(0), timestamp=ts, raw_text=text))

    # Sort oldest first
    messages.sort(key=lambda m: m.timestamp)
    return messages


# ── URL cleaning ───────────────────────────────────────────────────


def strip_tracking_params(url: str) -> str:
    """Remove tracking parameters (fbclid, utm_*, mibextid, etc.) from a URL."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}

    # Also strip fragment if it's empty or just whitespace
    fragment = parsed.fragment.strip() if parsed.fragment else ""

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/") if parsed.path != "/" else parsed.path,
        parsed.params,
        urlencode(cleaned, doseq=True) if cleaned else "",
        fragment,
    ))


def _clean_extracted_url(url: str) -> str:
    """Clean a URL extracted from post text (strip markdown artifacts, tracking)."""
    # Truncate at markdown link artifacts: ](
    if "](" in url:
        url = url[: url.index("](")]
    # Truncate at common trailing noise
    url = url.rstrip(".,;:)>]}")
    return strip_tracking_params(url)


def _is_facebook_url(url: str) -> bool:
    """Check if a URL is a Facebook domain."""
    netloc = urlparse(url).netloc.lower()
    return any(netloc == d or netloc.endswith("." + d) for d in _FB_DOMAINS)


def _is_noise_url(url: str) -> bool:
    """Check if a URL is from a noise domain we want to filter out."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    # Exact match or bare domain with no meaningful path
    if netloc in _NOISE_DOMAINS:
        return True
    # instagram.com with no meaningful path
    if "instagram.com" in netloc and (not parsed.path or parsed.path == "/"):
        return True
    return False


def _is_shortener(url: str) -> bool:
    """Check if a URL is a known shortener."""
    netloc = urlparse(url).netloc.lower()
    return netloc in _SHORTENER_DOMAINS


def classify_url(url: str) -> str:
    """Classify a URL: 'facebook', 'direct', or 'noise'."""
    if _is_facebook_url(url):
        return "facebook"
    if _is_noise_url(url):
        return "noise"
    return "direct"


# ── Facebook resolution ───────────────────────────────────────────


def _extract_post_text(html: str) -> str:
    """Extract post text from Facebook page HTML.

    Tries the JSON-embedded message.text field first, falls back to
    the <meta name="description"> tag.
    """
    # Strategy 1: JSON "message":{"text":"..."} in server-rendered HTML
    msgs = re.findall(r'"message":\s*\{[^}]*"text":\s*"([^"]*)"', html)
    if msgs:
        text = msgs[0]
        text = text.replace("\\/", "/").replace("\\n", "\n").replace("\\u0025", "%")
        return text

    # Strategy 2: meta description
    desc = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
    if desc:
        return desc.group(1)

    return ""


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract non-Facebook external URLs from post text.

    Handles both protocol-prefixed URLs and bare domain URLs like
    'arxiv.org/abs/...' or 'github.com/user/repo'.
    """
    urls: list[str] = []

    # Protocol-prefixed URLs
    for u in _URL_RE.findall(text):
        cleaned = _clean_extracted_url(u)
        if cleaned and not _is_facebook_url(cleaned) and not _is_noise_url(cleaned):
            urls.append(cleaned)

    # Bare domain URLs (without https://)
    for match in _BARE_DOMAIN_RE.finditer(text):
        domain = match.group(1)
        path = match.group(2)
        bare = f"https://{domain}{path}"
        cleaned = _clean_extracted_url(bare)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)

    return urls


def _follow_redirect(url: str, timeout: float = 10) -> str | None:
    """Follow redirects for URL shorteners. Returns final URL or None."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    }
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        final = str(r.url)
        if final != url and not _is_facebook_url(final):
            return _clean_extracted_url(final)
    except Exception:
        pass
    return None


def resolve_facebook_url(url: str, timeout: float = 15) -> ResolvedURL:
    """Fetch a Facebook URL and extract the external link from the post.

    Uses the facebookexternalhit User-Agent to get server-rendered
    content including the post text in a JSON message.text field.
    """
    # First check if the target URL is encoded in query params (l.facebook.com/l.php?u=...)
    parsed = urlparse(url)
    if parsed.netloc in ("l.facebook.com", "lm.facebook.com"):
        params = parse_qs(parsed.query)
        if "u" in params:
            decoded = unquote(params["u"][0])
            if not _is_facebook_url(decoded):
                return ResolvedURL(
                    url=_clean_extracted_url(decoded),
                    original_url=url,
                    description="",
                    category="resolved",
                    timestamp=datetime.now(timezone.utc),
                )

    # Fetch the page
    headers = {"User-Agent": "facebookexternalhit/1.1", "Accept": "text/html"}
    req = urllib.request.Request(url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return ResolvedURL(
            url=url,
            original_url=url,
            description=f"Fetch error: {e}",
            category="error",
            timestamp=datetime.now(timezone.utc),
        )

    # Extract post text and look for external URLs
    post_text = _extract_post_text(html)
    external_urls = _extract_urls_from_text(post_text)

    # Try to resolve shortener URLs found in the post
    resolved_urls: list[str] = []
    for eu in external_urls:
        if _is_shortener(eu):
            final = _follow_redirect(eu)
            if final:
                resolved_urls.append(final)
            else:
                resolved_urls.append(eu)
        else:
            resolved_urls.append(eu)

    if resolved_urls:
        return ResolvedURL(
            url=resolved_urls[0],  # Take the first (most prominent) external URL
            original_url=url,
            description=post_text[:300] if post_text else "",
            category="resolved",
            timestamp=datetime.now(timezone.utc),
        )

    if post_text and len(post_text) > 20:
        return ResolvedURL(
            url=url,
            original_url=url,
            description=post_text[:500],
            category="description_only",
            timestamp=datetime.now(timezone.utc),
        )

    return ResolvedURL(
        url=url,
        original_url=url,
        description="",
        category="empty",
        timestamp=datetime.now(timezone.utc),
    )


# ── Manifest I/O ──────────────────────────────────────────────────


def _load_manifest(weave_dir: Path) -> dict:
    path = weave_dir / _MANIFEST_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "imported_urls": {}}


def _save_manifest(weave_dir: Path, manifest: dict) -> None:
    path = weave_dir / _MANIFEST_NAME
    weave_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ── Queue item creation ───────────────────────────────────────────


def _build_queue_body(resolved: ResolvedURL, msg: Message) -> str:
    """Build the note body for a research queue item."""
    parts: list[str] = []
    parts.append(resolved.url)
    parts.append("")

    if resolved.description:
        parts.append("## Context")
        parts.append(resolved.description)
        parts.append("")

    parts.append("## Source")
    parts.append(f"- **From**: Messenger self-chat")
    parts.append(f"- **Date sent**: {msg.timestamp.strftime('%Y-%m-%d')}")
    if resolved.original_url != resolved.url:
        parts.append(f"- **Original URL**: {resolved.original_url}")

    return "\n".join(parts)


def _title_from_url(url: str, description: str = "") -> str:
    """Generate a descriptive title for a queue item."""
    parsed = urlparse(url)

    if "arxiv.org" in parsed.netloc:
        # Extract arxiv ID
        match = re.search(r"(\d{4}\.\d{4,5})", parsed.path)
        if match:
            return f"arxiv {match.group(1)}"
    if "github.com" in parsed.netloc:
        # Extract owner/repo
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            return f"GitHub: {parts[0]}/{parts[1]}"

    # Fall back to description snippet or domain
    if description:
        # First sentence or first 60 chars
        first_line = description.split("\n")[0].strip()
        if len(first_line) > 60:
            first_line = first_line[:57] + "..."
        return first_line

    return f"Link from {parsed.netloc}"


# ── Main import ────────────────────────────────────────────────────


def import_messenger(
    config: Config | None = None,
    json_path: Path | None = None,
    dry_run: bool = False,
    resolve: bool = True,
    since: str = "",
    until: str = "",
) -> dict:
    """Import Messenger self-chat links into the research queue.

    Args:
        config: Vault config.
        json_path: Path to the Messenger export JSON.
        dry_run: Parse and report without writing or fetching.
        resolve: If False, skip Facebook URL resolution (only queue direct URLs).
        since: Only import messages on or after this date (YYYY-MM-DD).
        until: Only import messages on or before this date (YYYY-MM-DD).

    Returns:
        Stats dict.
    """
    config = config or load_config()

    if not json_path or not json_path.exists():
        return {"error": f"File not found: {json_path}"}

    # Parse messages
    print(f"Parsing {json_path}...")
    messages = parse_messages(json_path)
    print(f"Found {len(messages)} messages with URLs.")

    # Apply date filters
    if since:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        messages = [m for m in messages if m.timestamp >= since_dt]
    if until:
        until_dt = datetime.strptime(until, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        messages = [m for m in messages if m.timestamp <= until_dt]

    if not messages:
        print("No messages after filtering.")
        return {"total": 0, "queued": 0, "skipped": 0, "errors": 0}

    print(f"Processing {len(messages)} messages after date filters.")

    # Phase 1: Classify all URLs
    direct: list[tuple[Message, str]] = []  # (msg, cleaned_url)
    facebook: list[Message] = []
    noise_count = 0

    for msg in messages:
        cleaned = strip_tracking_params(msg.url)
        cat = classify_url(cleaned)
        if cat == "direct":
            direct.append((msg, cleaned))
        elif cat == "facebook":
            facebook.append(msg)
        else:
            noise_count += 1

    print(f"\n── Classification ──────────────────────────────────")
    print(f"  Direct (non-Facebook):  {len(direct)}")
    print(f"  Facebook (need resolve): {len(facebook)}")
    print(f"  Noise (filtered out):    {noise_count}")

    if dry_run:
        return _dry_run_report(direct, facebook, noise_count)

    # Phase 2: Resolve Facebook URLs
    resolved_items: list[tuple[Message, ResolvedURL]] = []

    # Add direct URLs as pre-resolved
    for msg, cleaned in direct:
        resolved_items.append((
            msg,
            ResolvedURL(
                url=cleaned,
                original_url=msg.url,
                description="",
                category="direct",
                timestamp=msg.timestamp,
            ),
        ))

    if resolve and facebook:
        print(f"\n── Resolving {len(facebook)} Facebook URLs ─────────────")
        resolved_count = 0
        desc_only_count = 0
        empty_count = 0
        error_count = 0

        for i, msg in enumerate(facebook, 1):
            result = resolve_facebook_url(msg.url)
            result.timestamp = msg.timestamp

            if result.category == "resolved":
                resolved_count += 1
                resolved_items.append((msg, result))
            elif result.category == "description_only":
                desc_only_count += 1
                resolved_items.append((msg, result))
            elif result.category == "error":
                error_count += 1
            else:
                empty_count += 1

            # Progress every 25
            if i % 25 == 0 or i == len(facebook):
                print(
                    f"  [{i}/{len(facebook)}] "
                    f"resolved={resolved_count} desc_only={desc_only_count} "
                    f"empty={empty_count} errors={error_count}"
                )

            time.sleep(1.0)  # rate limit

        print(f"\n  Facebook resolution complete:")
        print(f"    Resolved (has URL):     {resolved_count}")
        print(f"    Description only:       {desc_only_count}")
        print(f"    Empty (no content):     {empty_count}")
        print(f"    Errors:                 {error_count}")
    elif not resolve:
        print(f"\n  Skipping Facebook resolution (--no-resolve)")

    # Phase 3: Create queue items
    print(f"\n── Creating queue items ─────────────────────────────")
    vm = VaultManager(config=config)
    vm.ensure_dirs()

    manifest = _load_manifest(config.weave_dir)
    imported_urls = manifest.get("imported_urls", {})

    stats = {
        "total": len(messages),
        "queued": 0,
        "queued_resolved": 0,
        "queued_needs_url": 0,
        "skipped": 0,
        "noise": noise_count,
        "errors": 0,
    }

    for msg, resolved in resolved_items:
        # Dedup key: resolved URL for direct/resolved, original FB URL for description_only
        dedup_key = resolved.url if resolved.category in ("direct", "resolved") else resolved.original_url
        if dedup_key in imported_urls:
            stats["skipped"] += 1
            continue

        # Build queue item
        tags = ["todo", "research"]
        if resolved.category == "description_only":
            tags.append("needs-url")

        title = _title_from_url(resolved.url, resolved.description)
        body = _build_queue_body(resolved, msg)

        try:
            path = vm.create_note(
                note_type=NoteType.NOTE,
                title=title,
                body=body,
                tags=tags,
            )

            # Index immediately
            idx = Indexer(config=config)
            idx.index_file(path)
            idx.close()

            imported_urls[dedup_key] = str(path.name)
            stats["queued"] += 1
            if resolved.category == "description_only":
                stats["queued_needs_url"] += 1
            else:
                stats["queued_resolved"] += 1

        except Exception as e:
            stats["errors"] += 1
            print(f"  Error creating queue item for {resolved.url[:60]}: {e}")

        # Save manifest periodically
        if stats["queued"] % 50 == 0 and stats["queued"] > 0:
            manifest["imported_urls"] = imported_urls
            _save_manifest(config.weave_dir, manifest)

    # Final manifest save
    manifest["imported_urls"] = imported_urls
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["source_file"] = str(json_path)
    _save_manifest(config.weave_dir, manifest)

    print(f"\n── Done ────────────────────────────────────────────")
    print(f"  Queued (with URL):    {stats['queued_resolved']}")
    print(f"  Queued (needs URL):   {stats['queued_needs_url']}")
    print(f"  Skipped (duplicate):  {stats['skipped']}")
    print(f"  Noise (filtered):     {stats['noise']}")
    print(f"  Errors:               {stats['errors']}")

    return stats


def _dry_run_report(
    direct: list[tuple[Message, str]],
    facebook: list[Message],
    noise_count: int,
) -> dict:
    """Print a summary without fetching or writing."""
    total = len(direct) + len(facebook) + noise_count

    print(f"\n── Dry Run Report ──────────────────────────────────\n")
    print(f"  Total messages:          {total}")
    print(f"  Direct (ready to queue): {len(direct)}")
    print(f"  Facebook (need resolve): {len(facebook)}")
    print(f"  Noise (will filter):     {noise_count}")
    print(f"  Est. resolve time:       ~{len(facebook)} seconds ({len(facebook) // 60} min)")

    # Show date range
    all_msgs = [m for m, _ in direct] + facebook
    if all_msgs:
        oldest = min(m.timestamp for m in all_msgs)
        newest = max(m.timestamp for m in all_msgs)
        print(f"  Date range:              {oldest.strftime('%Y-%m-%d')} → {newest.strftime('%Y-%m-%d')}")

    # Sample direct URLs
    if direct:
        print(f"\n  Sample direct URLs:")
        for msg, url in direct[:10]:
            print(f"    {msg.timestamp.strftime('%Y-%m-%d')}  {url[:80]}")
        if len(direct) > 10:
            print(f"    ... and {len(direct) - 10} more")

    # Facebook URL breakdown
    share_p = sum(1 for m in facebook if "/share/p/" in m.url)
    share = sum(1 for m in facebook if "/share/" in m.url and "/share/p/" not in m.url)
    other = len(facebook) - share_p - share
    print(f"\n  Facebook URL types:")
    print(f"    share/p/ (post links):   {share_p}")
    print(f"    share/ (photo/other):    {share}")
    print(f"    other (story/photo):     {other}")

    return {
        "total": total,
        "direct": len(direct),
        "facebook": len(facebook),
        "noise": noise_count,
        "dry_run": True,
    }
