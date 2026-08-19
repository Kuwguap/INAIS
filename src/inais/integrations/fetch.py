"""Fetch a web page and pull the readable text out of it.

Deliberately dependency-free: a readability library would extract better, but this runs on a
512 MB instance for one user saving the occasional article, and the failure mode of a lighter
extractor (some navigation text survives) is far cheaper than another wheel to build. If
extraction quality ever matters more than footprint, swap `html_to_text` for trafilatura —
nothing else needs to change.

Fetched pages are DATA. They are summarised and embedded, never followed as instructions.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=25)
MAX_BYTES = 3 * 1024 * 1024
USER_AGENT = ("Mozilla/5.0 (compatible; INAIS-personal-assistant/1.0; "
              "single-user read-it-later)")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Everything that is furniture rather than content.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|svg|iframe|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_BLOCK_END = re.compile(
    r"</(p|div|section|article|li|h[1-6]|br|tr|blockquote)\s*>|<br\s*/?>",
    re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
MIN_PARAGRAPH_CHARS = 40


MAX_REDIRECTS = 5


class FetchError(Exception):
    pass


def ip_is_forbidden(ip: str) -> bool:
    """Private, loopback, link-local, metadata — everything a server-side fetch must not hit."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


async def _assert_public(url: str) -> None:
    """SSRF guard: the URL must be http(s) and every resolved address must be public.

    The fetcher runs server-side with the platform's network access; without this, a
    forwarded link (or a redirect from one) could read localhost services or the cloud
    metadata endpoint and feed the result back into chat as an "article".
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https links can be saved")
    host = parts.hostname or ""
    if not host:
        raise FetchError("that URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or 80)
    except OSError as e:
        raise FetchError(f"couldn't resolve {host}") from e
    addresses = {info[4][0] for info in infos}
    if not addresses or any(ip_is_forbidden(ip) for ip in addresses):
        raise FetchError("that address isn't reachable from here")


@dataclass
class Page:
    url: str
    title: str
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,);]") for u in URL_RE.findall(text or "")]


MAX_ANNOTATION_CHARS = 25


def is_link_message(text: str) -> bool:
    """True when the message is essentially just a link.

    A URL inside a sentence is part of a conversation ("should I read this before Friday?")
    and must stay with the chat handler; a bare forwarded link, optionally with a short note
    like "read later", is a save request. Anything phrased as a question is a question —
    silently filing it instead of answering would be the worse failure of the two.
    """
    text = text or ""
    urls = extract_urls(text)
    if len(urls) != 1 or "?" in text:
        return False
    remainder = text.replace(urls[0], "").strip()
    return len(remainder) <= MAX_ANNOTATION_CHARS


def html_to_text(raw: str) -> tuple[str, str]:
    """(title, body text). Keeps paragraph-sized blocks, drops navigation furniture."""
    title = ""
    match = _TITLE_RE.search(raw) or _H1_RE.search(raw)
    if match:
        title = html.unescape(_TAG_RE.sub("", match.group(1))).strip()

    body = _DROP_BLOCKS.sub(" ", raw)
    body = _BLOCK_END.sub("\n", body)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)

    paragraphs: list[str] = []
    for line in body.splitlines():
        cleaned = re.sub(r"[ \t\xa0]+", " ", line).strip()
        # short lines are almost always menu items, bylines or button labels
        if len(cleaned) >= MIN_PARAGRAPH_CHARS:
            paragraphs.append(cleaned)

    deduped: list[str] = []
    seen: set[str] = set()
    for para in paragraphs:
        if para not in seen:      # boilerplate repeats across a page
            seen.add(para)
            deduped.append(para)
    return title, "\n\n".join(deduped)


async def fetch_page(url: str) -> Page:
    """Download and extract. Raises FetchError with a message worth showing the user."""
    if not URL_RE.fullmatch(url):
        raise FetchError("that doesn't look like a URL")
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            # follow redirects by hand so EVERY hop is validated, not just the first —
            # a public page redirecting to an internal address is the classic SSRF path
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await _assert_public(current)
                async with session.get(current, headers={"User-Agent": USER_AGENT},
                                       allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise FetchError("a redirect with no destination")
                        current = urljoin(current, location)
                        continue
                    if resp.status >= 400:
                        raise FetchError(f"the site returned {resp.status}")
                    content_type = resp.headers.get("Content-Type", "")
                    if "html" not in content_type and "text" not in content_type:
                        raise FetchError(
                            f"that's {content_type.split(';')[0] or 'not a web page'}, "
                            f"not an article")
                    raw = await resp.content.read(MAX_BYTES)
                    final_url = current
                    break
            else:
                raise FetchError("too many redirects")
    except aiohttp.ClientError as e:
        raise FetchError(f"couldn't reach it ({type(e).__name__})") from e
    except TimeoutError as e:
        raise FetchError("it took too long to respond") from e

    title, text = html_to_text(raw.decode("utf-8", errors="replace"))
    if len(text) < 200:
        raise FetchError("I couldn't find readable text on that page "
                         "(it may need JavaScript or a login)")
    return Page(url=final_url, title=title or final_url, text=text)
