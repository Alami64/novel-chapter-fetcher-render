"""
FastAPI backend – scrapes Chinese web novel chapters using Playwright (headless Chromium).
GET /api/chapter?url=<chapter_url>
Returns JSON: { text: str, next_url: str | null }

Cloud-deployable version: runs Playwright headless in a Docker container.
Uses sync API in a thread pool for compatibility.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from threading import Lock

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright, Browser, BrowserContext
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

# ── Thread pool – single worker to avoid Playwright greenlet thread-binding issues ─
_executor = ThreadPoolExecutor(max_workers=1)

# ── Global browser state (accessed only from thread pool) ────────────
_pw = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_lock = Lock()


def _ensure_browser() -> BrowserContext:
    """Lazily start Playwright + Chromium (called inside a worker thread)."""
    global _pw, _browser, _context
    with _lock:
        if _context is None or _browser is None or not _browser.is_connected():
            # Clean up any stale state
            try:
                if _context:
                    _context.close()
            except Exception:
                pass
            try:
                if _browser:
                    _browser.close()
            except Exception:
                pass
            _context = None
            _browser = None
            if _pw is None:
                _pw = sync_playwright().start()
            _browser = _pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            _context = _browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
    return _context


def _shutdown_browser():
    """Shut down Playwright resources (called once at app shutdown)."""
    global _pw, _browser, _context
    with _lock:
        if _context:
            _context.close()
            _context = None
        if _browser:
            _browser.close()
            _browser = None
        if _pw:
            _pw.stop()
            _pw = None


def _force_reset_browser():
    """Force-reset the browser if it's in a bad state."""
    global _browser, _context
    with _lock:
        try:
            if _context:
                _context.close()
        except Exception:
            pass
        try:
            if _browser:
                _browser.close()
        except Exception:
            pass
        _context = None
        _browser = None


# ── Content selectors ────────────────────────────────────────────────
CONTENT_SELECTORS = [
    {"class_": "txtnav"},          # 69shuba.com
    {"id": "chaptercontent"},
    {"id": "content"},
    {"id": "booktxt"},
    {"id": "htmlContent"},
    {"id": "TextContent"},
    {"class_": "chapter_content"},
    {"class_": "read-content"},
    {"class_": "content"},
    {"class_": "novel-content"},
    {"class_": "articlecontent"},
    {"class_": "txt"},
    {"class_": "readcontent"},
    {"class_": "chapter-content"},
    {"class_": "p-content"},
]

CSS_SELECTORS: list[str] = []
for _s in CONTENT_SELECTORS:
    if "id" in _s:
        CSS_SELECTORS.append(f"#{_s['id']}")
    elif "class_" in _s:
        CSS_SELECTORS.append(f".{_s['class_']}")

NEXT_LINK_PATTERNS = [
    re.compile(r"下一[章页篇]", re.IGNORECASE),
    re.compile(r"next\s*chapter", re.IGNORECASE),
]


# ── HTML helpers ─────────────────────────────────────────────────────
def find_content(soup: BeautifulSoup) -> str | None:
    for sel in CONTENT_SELECTORS:
        div = soup.find("div", **sel)
        if div:
            for tag in div.find_all(["script", "style", "ins", "iframe"]):
                tag.decompose()

            paragraphs = div.find_all("p")
            if paragraphs:
                lines = [p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True)]
                if lines:
                    return "\n\n".join(lines)

            for br in div.find_all("br"):
                br.replace_with("\n")
            text = div.get_text()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                return "\n\n".join(lines)
    return None


def find_next_url(soup: BeautifulSoup, base_url: str) -> str | None:
    # Method 1: look for 下一章 / next chapter links
    for pattern in NEXT_LINK_PATTERNS:
        for a in soup.find_all("a", string=pattern):
            href = a.get("href")
            if href and href != "#" and "javascript" not in href.lower():
                return urljoin(base_url, href)
        for a in soup.find_all("a"):
            if a.string and pattern.search(a.string):
                href = a.get("href")
                if href and href != "#" and "javascript" not in href.lower():
                    return urljoin(base_url, href)
    # Method 2: id="next"
    for a in soup.find_all("a", id="next"):
        href = a.get("href")
        if href and href != "#":
            return urljoin(base_url, href)
    # Method 3: extract from JS bookinfo object (69shuba.com)
    for script in soup.find_all("script"):
        if script.string and "next_page" in script.string:
            m = re.search(r'next_page\s*:\s*["\']([^"\']+)["\']', script.string)
            if m:
                return urljoin(base_url, m.group(1))
    return None


# ── Sync scraping function (runs in thread pool) ────────────────────
def _scrape_chapter(url: str) -> dict:
    """Fetch a chapter using headless Chromium (blocking / sync)."""
    try:
        ctx = _ensure_browser()
        page = ctx.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            if response is None:
                raise RuntimeError("No response received from page")

            # Wait for Cloudflare / anti-bot challenge (up to ~6s)
            for _ in range(3):
                title = page.title()
                if "请稍候" not in title and "Just a moment" not in title:
                    break
                page.wait_for_timeout(2000)

            # Give JS-rendered content a moment to appear
            page.wait_for_timeout(800)

            # Wait for a known content container (shorter timeout)
            for css in CSS_SELECTORS:
                try:
                    page.wait_for_selector(css, timeout=2000)
                    break
                except Exception:
                    continue

            html = page.content()
        finally:
            page.close()

        soup = BeautifulSoup(html, "lxml")
        text = find_content(soup)
        if not text:
            raise ValueError("Could not extract chapter text from this page.")

        next_url = find_next_url(soup, url)
        return {"text": text, "next_url": next_url}

    except Exception:
        # If anything goes wrong, force-reset the browser so the next
        # request doesn't hang on a dead browser instance
        _force_reset_browser()
        raise


# ── FastAPI lifespan ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _shutdown_browser)
    _executor.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

# Allow frontend origins (Vercel + local dev)
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        FRONTEND_URL,
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health check ─────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "message": "Novel Chapter Fetcher API"}


# ── API endpoint ─────────────────────────────────────────────────────
@app.get("/api/chapter")
async def get_chapter(url: str = Query(..., description="Chapter URL to scrape")):
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, partial(_scrape_chapter, url)),
            timeout=60,
        )
    except asyncio.TimeoutError:
        _force_reset_browser()
        raise HTTPException(status_code=504, detail="Request timed out (60s). Please try again.")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {e}")
    return result
