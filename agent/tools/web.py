"""Web tools: search and fetch."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote_plus

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


def web_search(query: str, max_results: int = 8) -> str:
    """Search the web using multiple strategies with automatic fallback."""
    for strategy in [_search_ddgs_cli, _search_google_scrape]:
        result = strategy(query, max_results)
        if result and not result.startswith("Error:"):
            return result
    return f"Web search unavailable for: {query}. Use web_fetch on a known URL instead."


def _search_ddgs_cli(query: str, max_results: int) -> str:
    """Try the ddgs Python library in a subprocess to avoid SSL issues in main process."""
    script = (
        "import json, sys\n"
        "try:\n"
        "    from ddgs import DDGS\n"
        "except ImportError:\n"
        "    from duckduckgo_search import DDGS\n"
        "q, m = sys.argv[1], int(sys.argv[2])\n"
        "with DDGS() as d:\n"
        "    r = list(d.text(q, max_results=m))\n"
        "print(json.dumps(r))\n"
    )
    try:
        proc = subprocess.run(
            ["python3", "-c", script, query, str(max_results)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if proc.returncode != 0:
            return ""
        results = json.loads(proc.stdout.strip())
        if not results:
            return ""
        return _format_results(results)
    except Exception:
        return ""


def _search_google_scrape(query: str, max_results: int) -> str:
    """Scrape Google search results via curl."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}"
    try:
        proc = subprocess.run(
            ["curl", "-sL", "--max-time", "10",
             "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
             url],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return ""

        html = proc.stdout
        results = []
        # Google wraps results in <a> tags with href containing /url?q=
        url_pattern = re.compile(r'/url\?q=(https?://[^&"]+)')
        title_pattern = re.compile(r'<h3[^>]*>(.*?)</h3>', re.DOTALL)

        urls = url_pattern.findall(html)
        titles = title_pattern.findall(html)

        seen_urls = set()
        for i, href in enumerate(urls[:max_results * 2]):
            if href in seen_urls:
                continue
            if any(skip in href for skip in ["google.com", "youtube.com/redirect", "accounts.google"]):
                continue
            seen_urls.add(href)
            title = ""
            if i < len(titles):
                title = re.sub(r"<[^>]+>", "", titles[i]).strip()
            results.append({"title": title or href[:80], "href": href, "body": ""})
            if len(results) >= max_results:
                break

        if not results:
            return ""
        return _format_results(results)
    except Exception:
        return ""


def _format_results(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        href = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"{i}. [{title}]({href})")
        if body:
            lines.append(f"   {body[:200]}")
        lines.append("")
    return "\n".join(lines).strip()


def web_fetch(url: str) -> str:
    """Fetch URL content. Uses httpx if available, falls back to curl."""
    if httpx is not None:
        return _fetch_httpx(url)
    return _fetch_curl(url)


def _fetch_httpx(url: str) -> str:
    try:
        resp = httpx.get(
            url, follow_redirects=True, timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SimpleBot/1.0)"},
        )
        resp.raise_for_status()
    except Exception:
        return _fetch_curl(url)

    content_type = resp.headers.get("content-type", "")
    return _format_response(resp.text, len(resp.content), content_type)


def _fetch_curl(url: str) -> str:
    try:
        proc = subprocess.run(
            ["curl", "-sL", "-A", "Mozilla/5.0 (compatible; SimpleBot/1.0)",
             "--max-time", "20", "-D", "-", url],
            capture_output=True, text=True, timeout=25, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Fetch timed out for {url}"

    if proc.returncode != 0:
        return f"Fetch error: curl returned {proc.returncode}"

    output = proc.stdout
    header_end = output.find("\r\n\r\n")
    if header_end > 0:
        headers = output[:header_end].lower()
        body = output[header_end + 4:]
    else:
        headers = ""
        body = output

    content_type = ""
    for line in headers.splitlines():
        if line.startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip()
            break

    return _format_response(body, len(body), content_type)


def _format_response(body: str, size: int, content_type: str) -> str:
    if "text/html" in content_type or body.strip().startswith("<"):
        return _html_to_text(body, max_chars=15_000)
    if "application/json" in content_type:
        return body[:15_000]
    if "text/" in content_type:
        return body[:15_000]
    if body and len(body) < 15_000:
        return body
    return f"Fetched {size} bytes of {content_type or 'unknown type'}"


_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _html_to_text(html: str, max_chars: int = 15_000) -> str:
    text = _TAG_RE.sub("", html)
    text = _HTML_TAG_RE.sub("\n", text)
    text = _WHITESPACE_RE.sub("\n\n", text).strip()
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:max_chars]
