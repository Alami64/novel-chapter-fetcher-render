"""
FastAPI backend – scrapes Chinese web novel chapters using Playwright (headless Chromium).
GET /api/chapter?url=<chapter_url>
Returns JSON: { text: str, next_url: str | null }

Cloud-deployable version: runs Playwright headless in a Docker container.
Uses Playwright async API directly – no thread pool needed on Linux.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, Browser, BrowserContext
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re

# ── Global browser state ─────────────────────────────────────────────
_pw = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_browser_lock = asyncio.Lock()


async def _ensure_browser() -> BrowserContext:
    """Lazily start Playwright + Chromium."""
    global _pw, _browser, _context
    async with _browser_lock:
        if _context is None or _browser is None or not _browser.is_connected():
            # Clean up stale state
            try:
                if _context:
                    await _context.close()
            except Exception:
                pass
            try:
                if _browser:
                    await _browser.close()
            except Exception:
                pass
            _context = None
            _browser = None
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            _context = await _browser.new_context(
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


async def _shutdown_browser():
    """Shut down Playwright resources."""
    global _pw, _browser, _context
    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _pw:
        await _pw.stop()
        _pw = None


async def _force_reset_browser():
    """Force-reset the browser if it's in a bad state."""
    global _browser, _context
    async with _browser_lock:
        try:
            if _context:
                await _context.close()
        except Exception:
            pass
        try:
            if _browser:
                await _browser.close()
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
    for a in soup.find_all("a", id="next"):
        href = a.get("href")
        if href and href != "#":
            return urljoin(base_url, href)
    for script in soup.find_all("script"):
        if script.string and "next_page" in script.string:
            m = re.search(r'next_page\s*:\s*["\']([^"\']+)["\']', script.string)
            if m:
                return urljoin(base_url, m.group(1))
    return None


# ── Async scraping function ──────────────────────────────────────────
async def _scrape_chapter(url: str) -> dict:
    """Fetch a chapter using headless Chromium (async)."""
    try:
        ctx = await _ensure_browser()
        page = await ctx.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            if response is None:
                raise RuntimeError("No response received from page")

            # Wait for Cloudflare / anti-bot challenge (up to ~6s)
            for _ in range(3):
                title = await page.title()
                if "请稍候" not in title and "Just a moment" not in title:
                    break
                await page.wait_for_timeout(2000)

            # Give JS-rendered content a moment to appear
            await page.wait_for_timeout(800)

            # Wait for a known content container
            for css in CSS_SELECTORS:
                try:
                    await page.wait_for_selector(css, timeout=2000)
                    break
                except Exception:
                    continue

            html = await page.content()
        finally:
            await page.close()

        soup = BeautifulSoup(html, "lxml")
        text = find_content(soup)
        if not text:
            raise ValueError("Could not extract chapter text from this page.")

        next_url = find_next_url(soup, url)
        return {"text": text, "next_url": next_url}

    except Exception:
        await _force_reset_browser()
        raise


# ── FastAPI lifespan ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await _shutdown_browser()


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
    try:
        result = await asyncio.wait_for(
            _scrape_chapter(url),
            timeout=120,
        )
    except asyncio.TimeoutError:
        await _force_reset_browser()
        raise HTTPException(status_code=504, detail="Request timed out (120s). Please try again.")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {e}")
    return result
