#!/usr/bin/env python3
"""
Google Maps Scraper using crawl4ai (v0.8.6)

Scrapes business listings from Google Maps based on city, country, and
search query. Uses Playwright under the hood via crawl4ai's AsyncWebCrawler
with VirtualScrollConfig for infinite-scroll handling.

Usage (single search):
    python scraper.py --city "Guadalajara" --country "Mexico" --query "auto repair"
    python scraper.py --city "NYC" --country "USA" --query "coffee shop" --max-results 50

Usage (grid search – covers the whole city):
    python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz"
    python scraper.py --grid --bbox 20.55 20.75 -103.45 -103.20 --query "taller mecanico" --zoom 14 --rows 8 --cols 10
"""

import asyncio
import json
from datetime import datetime
import csv
import logging
import re
import random
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Iterator, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.async_configs import VirtualScrollConfig
from crawl4ai.cache_context import CacheMode

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gmaps_scraper")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BusinessInfo:
    """A single business listing extracted from Google Maps."""

    name: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    category: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    google_maps_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def dedup_key(self) -> str:
        """Unique key for deduplication."""
        return self.google_maps_url or self.name or ""


# ---------------------------------------------------------------------------
# JavaScript snippets
# ---------------------------------------------------------------------------

# Dismiss Google consent / cookie dialogs before interacting
JS_DISMISS_CONSENT = """
(function() {
    // Selector-based (most reliable)
    const selectors = [
        '#L2AGLb',
        'button[jsname="b3VHJd"]',
        'button[aria-label*="Accept"]',
        'button[aria-label*="Agree"]',
        '.sy4vM',
        'button[jsname="higCR"]',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) { btn.click(); return 'consent_dismissed:' + sel; }
    }
    // Text-based fallback (handles locale variations)
    const pattern = /^(accept all|i agree|agree|accept|akzeptieren|tout accepter)$/i;
    for (const btn of document.querySelectorAll('button, a[role="button"]')) {
        if (pattern.test(btn.textContent.trim())) { btn.click(); return 'consent_dismissed:text'; }
    }
    return 'no_consent_found';
})();
"""

# Accept any remaining overlays / "before you continue" dialogs
JS_CLOSE_OVERLAYS = """
(function() {
    // Close any modal overlays that might block interaction
    const overlay = document.querySelector('div[role="dialog"] button[aria-label*="Close"]');
    if (overlay) { overlay.click(); return 'overlay_closed'; }
    return 'ok';
})();
"""

# Scroll the left-side results panel by its full height
JS_SCROLL_PANEL = """
(function() {
    const feed = document.querySelector('div[role="feed"]');
    if (!feed) return { scrolled: false, height: 0 };
    const prevHeight = feed.scrollHeight;
    feed.scrollTo({ top: feed.scrollHeight, behavior: 'smooth' });
    return { scrolled: true, prevHeight: prevHeight, newHeight: feed.scrollHeight };
})();
"""

# Check whether new content has loaded after a scroll
JS_WAIT_FOR_MORE = """
() => {
    const feed = document.querySelector('div[role="feed"]');
    return feed !== null && feed.querySelectorAll('a[href*="/maps/place/"]').length > 0;
}
"""

# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _text(el) -> Optional[str]:
    """Return stripped text content of a BS4 element, or None."""
    return el.get_text(strip=True) if el else None


def _parse_rating(element) -> Optional[float]:
    """Extract numeric star rating from a business card element."""
    # Approach 1 – aria-label on the star container (most stable)
    for tag in element.find_all(attrs={"aria-label": True}):
        label = tag["aria-label"]
        m = re.search(r"([\d.]+)\s+star", label, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass

    # Approach 2 – common class names for the rating text
    for cls in ["MW4etd", "ZkP5Je"]:
        el = element.find(class_=cls)
        if el:
            try:
                return float(el.get_text(strip=True).replace(",", "."))
            except ValueError:
                pass

    return None


def _parse_reviews_count(element) -> Optional[int]:
    """Extract integer review count from a business card element."""
    # Approach 1 – aria-label
    for tag in element.find_all(attrs={"aria-label": True}):
        label = tag["aria-label"]
        m = re.search(r"([\d,]+)\s+review", label, re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # Approach 2 – common class names  (parenthesised number)
    for cls in ["UY7F9", "e4rVHe"]:
        el = element.find(class_=cls)
        if el:
            digits = re.sub(r"[^\d]", "", el.get_text(strip=True))
            if digits:
                return int(digits)

    return None


def _parse_category_and_address(element) -> tuple[Optional[str], Optional[str]]:
    """
    Extract category and address from the metadata rows inside a card.

    Google Maps typically renders these as small text blocks separated by
    bullet "·" characters.  The category comes first (no digits), the
    address usually contains a number or street keywords.
    """
    category: Optional[str] = None
    address: Optional[str] = None

    # Collect all inner text segments from W4Efsd rows
    for row in element.select("div.W4Efsd, div[class*='W4Efsd']"):
        text = row.get_text(separator="·", strip=True)
        parts = [p.strip() for p in text.split("·") if p.strip()]
        for part in parts:
            # Skip very short or very long fragments
            if len(part) < 3 or len(part) > 120:
                continue
            # Skip pure numeric / decimal strings (e.g. ratings "4.1", distances "2.3 km")
            if re.match(r"^[\d][\d\s.,kmKM]*$", part):
                continue
            # Skip parenthesised review counts "(320)"
            if re.match(r"^\([\d,]+\)$", part):
                continue
            # Category: no digits, short, likely a business-type label
            if category is None and not re.search(r"\d", part):
                category = part
            # Address: digits mixed with actual letters (not just decimals/distances)
            elif address is None and re.search(r"\d", part):
                non_digits = re.sub(r"[\d\s.,\-#]", "", part)
                if len(non_digits) >= 2:   # must contain real text letters
                    address = part

    return category, address


def _parse_phone(element) -> Optional[str]:
    """Look for a phone number string inside the card."""
    # Common international + local phone patterns
    phone_re = re.compile(r"^\+?[\d][\d\s\-\(\).]{6,}$")

    for tag in element.find_all(string=phone_re):
        return tag.strip()

    # Fallback: aria-label containing "phone"
    for tag in element.find_all(attrs={"aria-label": re.compile(r"phone|tel", re.I)}):
        m = re.search(r"[\+\d][\d\s\-\(\).]{6,}", tag["aria-label"])
        if m:
            return m.group().strip()

    return None


def _parse_website(element) -> Optional[str]:
    """Extract the website URL from a card if present."""
    # Explicit data-value="Website" link
    el = element.select_one('a[data-value="Website"]')
    if el:
        return el.get("href")

    # Any outbound link that is not a Google URL
    for a in element.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and "google" not in href and "goo.gl" not in href:
            return href

    return None


class _LocalProxyForwarder:
    """
    Minimal HTTP/CONNECT proxy that forwards to an authenticated upstream.

    Chromium has a known issue where it fails to authenticate with proxies in
    headless mode.  This forwarder listens on localhost (no auth required for
    Chromium) and injects Proxy-Authorization when tunneling to the upstream.
    """

    LOCAL_PORT = 18888

    def __init__(self, proxy_url: str):
        import base64
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        self._host = p.hostname
        self._port = p.port
        self._auth = (
            b"Proxy-Authorization: Basic "
            + base64.b64encode(f"{p.username}:{p.password}".encode())
            + b"\r\n"
        )
        self._server = None
        self.bytes_sent = 0      # browser → upstream (requests)
        self.bytes_recv = 0      # upstream → browser (responses)

    @property
    def total_bytes(self) -> int:
        return self.bytes_sent + self.bytes_recv

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)

    def log_usage(self, label: str = "") -> None:
        prefix = f"[{label}] " if label else ""
        logger.info(
            "%sProxy usage — sent: %.2f MB | recv: %.2f MB | total: %.2f MB",
            prefix,
            self.bytes_sent / (1024 * 1024),
            self.bytes_recv / (1024 * 1024),
            self.total_mb,
        )

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.LOCAL_PORT}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self.LOCAL_PORT
        )

    def stop(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(self, client_r, client_w):
        try:
            first_line = await client_r.readline()
            if not first_line:
                return
            headers: list[bytes] = []
            while True:
                line = await client_r.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                headers.append(line)

            up_r, up_w = await asyncio.open_connection(self._host, self._port)
            up_w.write(first_line)
            up_w.write(self._auth)
            for h in headers:
                if not h.lower().startswith(b"proxy-authorization"):
                    up_w.write(h)
            up_w.write(b"\r\n")
            await up_w.drain()

            resp = await up_r.readline()
            client_w.write(resp)
            while True:
                line = await up_r.readline()
                client_w.write(line)
                if line in (b"\r\n", b"\n", b""):
                    break
            await client_w.drain()

            await asyncio.gather(
                self._pipe(client_r, up_w, self, "sent"),
                self._pipe(up_r, client_w, self, "recv"),
            )
        except Exception:
            pass
        finally:
            client_w.close()

    @staticmethod
    async def _pipe(src, dst, counter: "_LocalProxyForwarder", direction: str):
        try:
            while chunk := await src.read(65536):
                if direction == "sent":
                    counter.bytes_sent += len(chunk)
                else:
                    counter.bytes_recv += len(chunk)
                dst.write(chunk)
                await dst.drain()
        except Exception:
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass


def _parse_coords_from_url(url: str) -> tuple[Optional[float], Optional[float]]:
    """Extract (latitude, longitude) from a Google Maps place URL.

    Tries two patterns in order:
      1. ``@lat,lng,zoom``  – present in the URL path for most results.
      2. ``!3d{lat}!4d{lng}`` – encoded in the data segment as fallback.
    """
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if m:
        return float(m.group(1)), float(m.group(2))

    lat_m = re.search(r"!3d(-?\d+\.\d+)", url)
    lng_m = re.search(r"!4d(-?\d+\.\d+)", url)
    if lat_m and lng_m:
        return float(lat_m.group(1)), float(lng_m.group(1))

    return None, None


def parse_business_card(card_element) -> Optional[BusinessInfo]:
    """
    Parse a single <div role="article"> or equivalent card element.

    Returns a populated BusinessInfo, or None if the element does not
    look like a business listing.
    """
    try:
        biz = BusinessInfo()

        # --- Name -----------------------------------------------------------
        for sel in [
            "span.fontHeadlineSmall",
            "div.qBF1Pd",
            "[class*='fontHeadline']",
            "h3",
        ]:
            el = card_element.select_one(sel)
            if el and el.get_text(strip=True):
                biz.name = el.get_text(strip=True)
                break

        if not biz.name:
            # Last resort: aria-label on the root anchor
            anchor = card_element.find("a", href=re.compile(r"/maps/place/"))
            if anchor and anchor.get("aria-label"):
                biz.name = anchor["aria-label"].strip()

        # --- Google Maps URL + coordinates ----------------------------------
        anchor = card_element.find("a", href=re.compile(r"/maps/place/"))
        if anchor:
            href = anchor.get("href", "")
            if href:
                biz.latitude, biz.longitude = _parse_coords_from_url(href)
                # Keep only the canonical place path, drop query params
                biz.google_maps_url = re.sub(r"\?.*$", "", href)

        # --- Rating / Reviews -----------------------------------------------
        biz.rating = _parse_rating(card_element)
        biz.reviews_count = _parse_reviews_count(card_element)

        # --- Category / Address ---------------------------------------------
        biz.category, biz.address = _parse_category_and_address(card_element)

        # --- Phone ----------------------------------------------------------
        biz.phone = _parse_phone(card_element)

        # --- Website --------------------------------------------------------
        biz.website = _parse_website(card_element)

        # Discard elements that yielded no name (probably not business cards)
        return biz if biz.name else None

    except Exception as exc:
        logger.debug("Error parsing card: %s", exc)
        return None


def extract_businesses_from_html(
    html: str,
    seen_keys: set,
) -> List[BusinessInfo]:
    """
    Parse all business cards from a full-page HTML snapshot.

    Args:
        html:      Raw HTML of the Google Maps search page.
        seen_keys: Set of already-collected dedup keys (mutated in-place).

    Returns:
        List of new BusinessInfo objects not previously seen.
    """
    soup = BeautifulSoup(html, "lxml")
    new_businesses: List[BusinessInfo] = []

    # The results feed container
    feed = soup.find("div", role="feed")
    if not feed:
        logger.debug("No div[role='feed'] found in HTML snapshot.")
        return new_businesses

    # Each result can be an article div or a direct anchor tag
    cards = feed.find_all("div", role="article")
    if not cards:
        # Fallback: grab top-level anchors that link to a place
        cards = feed.find_all("a", href=re.compile(r"/maps/place/"))

    logger.debug("Found %d raw card elements in snapshot.", len(cards))

    for card in cards:
        biz = parse_business_card(card)
        if biz and biz.dedup_key and biz.dedup_key not in seen_keys:
            seen_keys.add(biz.dedup_key)
            new_businesses.append(biz)

    return new_businesses


def has_reached_end_of_list(html: str) -> bool:
    """Return True when Google Maps indicates there are no more results."""
    # Google typically renders a message like "You've reached the end of the list."
    soup = BeautifulSoup(html, "lxml")
    body_text = soup.get_text(" ", strip=True).lower()
    markers = [
        "you've reached the end of the list",
        "end of the list",
        "no more results",
    ]
    return any(m in body_text for m in markers)


# ---------------------------------------------------------------------------
# Geographic grid support
# ---------------------------------------------------------------------------

CITY_BBOXES: Dict[str, Dict[str, float]] = {
    "guadalajara":     {"lat_min": 20.55, "lat_max": 20.75, "lon_min": -103.45, "lon_max": -103.20},
    "monterrey":       {"lat_min": 25.55, "lat_max": 25.85, "lon_min": -100.45, "lon_max": -100.20},
    "mexico city":     {"lat_min": 19.25, "lat_max": 19.60, "lon_min": -99.30,  "lon_max": -98.95},
    "ciudad de mexico":{"lat_min": 19.25, "lat_max": 19.60, "lon_min": -99.30,  "lon_max": -98.95},
    "tijuana":         {"lat_min": 32.40, "lat_max": 32.60, "lon_min": -117.15, "lon_max": -116.85},
    "puebla":          {"lat_min": 18.95, "lat_max": 19.15, "lon_min": -98.30,  "lon_max": -98.10},
    "toluca":          {"lat_min": 19.22, "lat_max": 19.38, "lon_min": -99.75,  "lon_max": -99.57},
    "leon":            {"lat_min": 21.03, "lat_max": 21.20, "lon_min": -101.75, "lon_max": -101.55},
    "juarez":          {"lat_min": 31.60, "lat_max": 31.80, "lon_min": -106.55, "lon_max": -106.35},
    "torreon":         {"lat_min": 25.45, "lat_max": 25.65, "lon_min": -103.55, "lon_max": -103.35},
    "queretaro":       {"lat_min": 20.52, "lat_max": 20.68, "lon_min": -100.45, "lon_max": -100.30},
    "san luis potosi": {"lat_min": 22.08, "lat_max": 22.22, "lon_min": -101.08, "lon_max": -100.92},
    "merida":          {"lat_min": 20.90, "lat_max": 21.05, "lon_min": -89.70,  "lon_max": -89.55},
    "mexicali":        {"lat_min": 32.55, "lat_max": 32.72, "lon_min": -115.55, "lon_max": -115.38},
    "aguascalientes":  {"lat_min": 21.82, "lat_max": 21.97, "lon_min": -102.35, "lon_max": -102.20},
    "cuernavaca":      {"lat_min": 18.87, "lat_max": 19.02, "lon_min": -99.30,  "lon_max": -99.18},
    "saltillo":        {"lat_min": 25.35, "lat_max": 25.55, "lon_min": -101.10, "lon_max": -100.92},
    "hermosillo":      {"lat_min": 29.02, "lat_max": 29.18, "lon_min": -111.08, "lon_max": -110.90},
    "culiacan":        {"lat_min": 24.74, "lat_max": 24.88, "lon_min": -107.50, "lon_max": -107.35},
    "chihuahua":       {"lat_min": 28.58, "lat_max": 28.75, "lon_min": -106.15, "lon_max": -105.97},
    "morelia":         {"lat_min": 19.65, "lat_max": 19.78, "lon_min": -101.25, "lon_max": -101.10},
    # United Kingdom
    "london":              {"lat_min": 51.28, "lat_max": 51.70, "lon_min": -0.51,   "lon_max":  0.33},
    "manchester":          {"lat_min": 53.38, "lat_max": 53.55, "lon_min": -2.35,   "lon_max": -2.10},
    "birmingham":          {"lat_min": 52.38, "lat_max": 52.57, "lon_min": -2.00,   "lon_max": -1.73},
    "glasgow":             {"lat_min": 55.78, "lat_max": 55.92, "lon_min": -4.40,   "lon_max": -4.10},
    "southampton":         {"lat_min": 50.87, "lat_max": 50.97, "lon_min": -1.48,   "lon_max": -1.32},
    "liverpool":           {"lat_min": 53.32, "lat_max": 53.48, "lon_min": -3.05,   "lon_max": -2.83},
    "bristol":             {"lat_min": 51.40, "lat_max": 51.53, "lon_min": -2.68,   "lon_max": -2.52},
    "sheffield":           {"lat_min": 53.30, "lat_max": 53.47, "lon_min": -1.60,   "lon_max": -1.35},
    "leeds":               {"lat_min": 53.72, "lat_max": 53.87, "lon_min": -1.70,   "lon_max": -1.45},
    "edinburgh":           {"lat_min": 55.88, "lat_max": 56.00, "lon_min": -3.35,   "lon_max": -3.10},
    "cardiff":             {"lat_min": 51.44, "lat_max": 51.55, "lon_min": -3.28,   "lon_max": -3.12},
    "leicester":           {"lat_min": 52.58, "lat_max": 52.68, "lon_min": -1.20,   "lon_max": -1.05},
    "stoke-on-trent":      {"lat_min": 52.98, "lat_max": 53.07, "lon_min": -2.22,   "lon_max": -2.10},
    "hull":                {"lat_min": 53.72, "lat_max": 53.80, "lon_min": -0.40,   "lon_max": -0.25},
    "plymouth":            {"lat_min": 50.35, "lat_max": 50.43, "lon_min": -4.18,   "lon_max": -4.05},
    "nottingham":          {"lat_min": 52.90, "lat_max": 53.00, "lon_min": -1.22,   "lon_max": -1.08},
    "bradford":            {"lat_min": 53.76, "lat_max": 53.85, "lon_min": -1.82,   "lon_max": -1.70},
    "belfast":             {"lat_min": 54.55, "lat_max": 54.65, "lon_min": -6.05,   "lon_max": -5.85},
    "portsmouth":          {"lat_min": 50.78, "lat_max": 50.85, "lon_min": -1.12,   "lon_max": -1.02},
    "barnsley":            {"lat_min": 53.52, "lat_max": 53.58, "lon_min": -1.52,   "lon_max": -1.45},
    "brighton and hove":   {"lat_min": 50.82, "lat_max": 50.88, "lon_min": -0.22,   "lon_max": -0.08},
    "swindon":             {"lat_min": 51.53, "lat_max": 51.60, "lon_min": -1.83,   "lon_max": -1.72},
    "derby":               {"lat_min": 52.88, "lat_max": 52.97, "lon_min": -1.55,   "lon_max": -1.43},
    "sunderland":          {"lat_min": 54.88, "lat_max": 54.95, "lon_min": -1.45,   "lon_max": -1.35},
    "wolverhampton":       {"lat_min": 52.56, "lat_max": 52.63, "lon_min": -2.17,   "lon_max": -2.07},
    # France
    "paris":               {"lat_min": 48.81, "lat_max": 48.91, "lon_min":  2.22,   "lon_max":  2.47},
    "marseille":           {"lat_min": 43.17, "lat_max": 43.38, "lon_min":  5.25,   "lon_max":  5.52},
    "lyon":                {"lat_min": 45.70, "lat_max": 45.82, "lon_min":  4.77,   "lon_max":  4.90},
    "toulouse":            {"lat_min": 43.54, "lat_max": 43.67, "lon_min":  1.33,   "lon_max":  1.50},
    "nice":                {"lat_min": 43.64, "lat_max": 43.74, "lon_min":  7.18,   "lon_max":  7.32},
    "nantes":              {"lat_min": 47.18, "lat_max": 47.28, "lon_min": -1.63,   "lon_max": -1.48},
    "bordeaux":            {"lat_min": 44.80, "lat_max": 44.90, "lon_min": -0.65,   "lon_max": -0.52},
    "strasbourg":          {"lat_min": 48.53, "lat_max": 48.63, "lon_min":  7.67,   "lon_max":  7.82},
    "montpellier":         {"lat_min": 43.57, "lat_max": 43.65, "lon_min":  3.82,   "lon_max":  3.93},
    "lille":               {"lat_min": 50.60, "lat_max": 50.68, "lon_min":  3.02,   "lon_max":  3.12},
    "rennes":              {"lat_min": 48.07, "lat_max": 48.14, "lon_min": -1.74,   "lon_max": -1.63},
    "toulon":              {"lat_min": 43.10, "lat_max": 43.18, "lon_min":  5.87,   "lon_max":  5.98},
    "rouen":               {"lat_min": 49.40, "lat_max": 49.48, "lon_min":  1.03,   "lon_max":  1.13},
    "aix-en-provence":     {"lat_min": 43.50, "lat_max": 43.57, "lon_min":  5.38,   "lon_max":  5.50},
    "clermont-ferrand":    {"lat_min": 45.75, "lat_max": 45.82, "lon_min":  3.05,   "lon_max":  3.14},
    "saint-denis":         {"lat_min": 48.92, "lat_max": 48.96, "lon_min":  2.33,   "lon_max":  2.38},
    "le mans":             {"lat_min": 47.98, "lat_max": 48.05, "lon_min":  0.17,   "lon_max":  0.25},
    "nimes":               {"lat_min": 43.80, "lat_max": 43.87, "lon_min":  4.33,   "lon_max":  4.43},
    "lyon-villeurbanne":   {"lat_min": 45.74, "lat_max": 45.78, "lon_min":  4.87,   "lon_max":  4.93},
    "saint-etienne":       {"lat_min": 45.40, "lat_max": 45.48, "lon_min":  4.37,   "lon_max":  4.47},
    "caen":                {"lat_min": 49.15, "lat_max": 49.22, "lon_min": -0.43,   "lon_max": -0.33},
    "nancy":               {"lat_min": 48.67, "lat_max": 48.73, "lon_min":  6.15,   "lon_max":  6.22},
    "orleans":             {"lat_min": 47.87, "lat_max": 47.93, "lon_min":  1.87,   "lon_max":  1.97},
    "argenteuil":          {"lat_min": 48.93, "lat_max": 48.97, "lon_min":  2.23,   "lon_max":  2.29},
    "montreuil":           {"lat_min": 48.85, "lat_max": 48.88, "lon_min":  2.43,   "lon_max":  2.47},
}


def generate_grid_cells(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    rows: int,
    cols: int,
) -> Iterator[Tuple[float, float]]:
    """
    Yield (center_lat, center_lng) for each cell in a rows×cols grid
    covering the given bounding box, in row-major order (top-to-bottom,
    left-to-right).
    """
    lat_step = (lat_max - lat_min) / rows
    lon_step = (lon_max - lon_min) / cols
    for row in range(rows):
        for col in range(cols):
            center_lat = lat_max - (row + 0.5) * lat_step
            center_lng = lon_min + (col + 0.5) * lon_step
            yield center_lat, center_lng


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------

class GoogleMapsScraper:
    """
    Async Google Maps business-listing scraper built on crawl4ai.

    Strategy
    --------
    1. Load the search URL via a full browser session.
    2. Dismiss any consent / cookie dialogs.
    3. Use VirtualScrollConfig to auto-scroll the results panel (fast path).
    4. If VirtualScrollConfig did not yield enough results, fall back to a
       manual JS-scroll loop using session_id + js_only=True.
    5. Parse the HTML snapshots with BeautifulSoup after each phase.

    Grid mode
    ---------
    scrape_grid() divides a bounding box into tiles and calls _scrape_single_tile()
    for each one, sharing a single browser session and seen_keys set across all tiles.
    """

    # How many consecutive scroll steps with zero new results before giving up
    STALE_STREAK_LIMIT = 3

    def __init__(
        self,
        headless: bool = True,
        max_results: Optional[int] = None,
        max_scroll_steps: int = 8,
        scroll_wait: float = 1.5,
        proxy: Optional[str] = None,
        phase2_threshold: int = 3,
        tile_stale_limit: int = 6,
        tile_timeout: float = 240,
    ):
        """
        Args:
            headless:          Run browser without a visible window.
            max_results:       Stop after collecting this many businesses (None = unlimited).
            max_scroll_steps:  Maximum scroll iterations per phase per tile.
            scroll_wait:       Base seconds to wait after each scroll for content to load.
            proxy:             Proxy URL, e.g. "http://user:pass@host:port".
            phase2_threshold:  Skip Phase 2 if Phase 1 already found this many new results.
                               Saves proxy bandwidth when Phase 1 is working well.
            tile_stale_limit:  Stop the grid after this many consecutive tiles that yield
                               zero new results (coverage saturated).
            tile_timeout:      Max seconds to wait for a single tile before skipping it.
                               Prevents a hung Playwright call from blocking the run overnight.
        """
        self.headless = headless
        self.max_results = max_results
        self.max_scroll_steps = max_scroll_steps
        self.scroll_wait = scroll_wait
        self.proxy = proxy
        self.phase2_threshold = phase2_threshold
        self.tile_stale_limit = tile_stale_limit
        self.tile_timeout = tile_timeout
        self._session_id = "gmaps_session"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def scrape(
        self,
        city: str,
        country: str,
        search_query: str,
    ) -> List[BusinessInfo]:
        """
        Scrape Google Maps for businesses matching *search_query* in *city*, *country*.

        Returns a deduplicated list of BusinessInfo objects.
        """
        search_url = self._build_url(city, country, search_query)
        logger.info("=== Google Maps Scraper starting ===")
        logger.info("Query  : %s in %s, %s", search_query, city, country)
        logger.info("URL    : %s", search_url)

        forwarder, proxy_url = await self._start_forwarder()
        browser_cfg = BrowserConfig(
            headless=self.headless,
            verbose=False,
            viewport_width=1280,
            viewport_height=900,
            enable_stealth=True,
            proxy=proxy_url,
        )

        seen_keys: set = set()
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            businesses = await self._scrape_single_tile(crawler, search_url, seen_keys)

        if forwarder:
            forwarder.log_usage("RESUMEN FINAL")
            forwarder.stop()

        logger.info("=== Scraping complete – %d businesses collected ===", len(businesses))
        return businesses

    async def scrape_grid(
        self,
        query: str,
        lat_min: float,
        lat_max: float,
        lon_min: float,
        lon_max: float,
        rows: int = 8,
        cols: int = 10,
        zoom: int = 14,
        checkpoint_fn: Optional[Any] = None,
        checkpoint_every: int = 10,
    ) -> List[BusinessInfo]:
        """
        Scrape Google Maps using a grid of coordinate-anchored tiles.

        Divides the bounding box into rows×cols cells and scrapes each one
        separately, sharing a single browser session and seen_keys set so
        businesses that appear in multiple tiles are counted only once.

        Args:
            query:   Raw search term, e.g. "reparacion automotriz".
            lat_min: Southern latitude boundary.
            lat_max: Northern latitude boundary.
            lon_min: Western longitude boundary.
            lon_max: Eastern longitude boundary.
            rows:    Number of grid rows (north-to-south divisions).
            cols:    Number of grid columns (west-to-east divisions).
            zoom:    Google Maps zoom level (13 ≈ 5 km, 14 ≈ 2.5 km visible diameter).

        Returns:
            Deduplicated list of BusinessInfo objects from all tiles.
        """
        import time

        total_tiles = rows * cols
        logger.info("=== Grid Scraper starting ===")
        logger.info("Query : %s", query)
        logger.info("BBox  : lat [%.4f, %.4f]  lon [%.4f, %.4f]", lat_min, lat_max, lon_min, lon_max)
        logger.info("Grid  : %d rows × %d cols = %d tiles  (zoom %d)", rows, cols, total_tiles, zoom)
        if self.proxy:
            safe_proxy = re.sub(r"://([^:]+:[^@]+)@", "://*****@", self.proxy)
            logger.info("Proxy : %s", safe_proxy)

        forwarder, proxy_url = await self._start_forwarder()
        browser_cfg = BrowserConfig(
            headless=self.headless,
            verbose=False,
            viewport_width=1280,
            viewport_height=900,
            enable_stealth=True,
            proxy=proxy_url,
        )

        businesses: List[BusinessInfo] = []
        seen_keys: set = set()
        start_time = time.monotonic()
        consecutive_empty = 0
        tile_num = 0

        try:
            async with AsyncWebCrawler(config=browser_cfg) as crawler:
                for tile_num, (center_lat, center_lng) in enumerate(
                    generate_grid_cells(lat_min, lat_max, lon_min, lon_max, rows, cols), 1
                ):
                    tile_url = self._build_grid_url(center_lat, center_lng, zoom, query)
                    logger.info(
                        "── Tile %d/%d  (%.5f, %.5f)  unique so far: %d",
                        tile_num, total_tiles, center_lat, center_lng, len(businesses),
                    )

                    try:
                        new = await asyncio.wait_for(
                            self._scrape_single_tile(crawler, tile_url, seen_keys),
                            timeout=self.tile_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Tile %d/%d colgado por más de %.0fs – saltando.",
                            tile_num, total_tiles, self.tile_timeout,
                        )
                        new = []
                    businesses.extend(new)

                    if len(new) == 0:
                        consecutive_empty += 1
                        logger.info(
                            "   +0 nuevos | total: %d | tiles vacíos consecutivos: %d/%d",
                            len(businesses), consecutive_empty, self.tile_stale_limit,
                        )
                        if consecutive_empty >= self.tile_stale_limit:
                            logger.info(
                                "Cobertura saturada (%d tiles consecutivos sin resultados nuevos) – deteniendo grid.",
                                consecutive_empty,
                            )
                            break
                    else:
                        consecutive_empty = 0
                        elapsed = time.monotonic() - start_time
                        avg = elapsed / tile_num
                        eta_min = avg * (total_tiles - tile_num) / 60
                        logger.info(
                            "   +%d nuevos | total: %d | ETA: ~%.0f min",
                            len(new), len(businesses), eta_min,
                        )

                    if forwarder:
                        logger.info(
                            "   proxy acumulado: %.2f MB (↑%.2f MB ↓%.2f MB)",
                            forwarder.total_mb,
                            forwarder.bytes_sent / (1024 * 1024),
                            forwarder.bytes_recv / (1024 * 1024),
                        )

                    if checkpoint_fn and tile_num % checkpoint_every == 0 and businesses:
                        logger.info("Checkpoint – guardando %d resultados (tile %d) …", len(businesses), tile_num)
                        checkpoint_fn(businesses)

                    if self.max_results and len(businesses) >= self.max_results:
                        businesses = businesses[: self.max_results]
                        logger.info("Reached max_results (%d) – stopping grid.", self.max_results)
                        break

                    # Polite inter-tile pause to reduce bot-detection risk
                    await asyncio.sleep(random.uniform(1.0, 2.5))

        except KeyboardInterrupt:
            logger.info(
                "Interrupción manual (Ctrl+C) – guardando %d resultados parciales …",
                len(businesses),
            )
            if checkpoint_fn and businesses:
                checkpoint_fn(businesses)

        if forwarder:
            forwarder.log_usage("RESUMEN FINAL")
            forwarder.stop()

        logger.info(
            "=== Grid complete – %d unique businesses across %d/%d tiles ===",
            len(businesses), tile_num, total_tiles,
        )
        return businesses

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _start_forwarder(self):
        """Start the local proxy forwarder if a proxy URL is configured.

        Returns (forwarder, proxy_url_for_browser). When no proxy is set,
        returns (None, None).  Chromium connects to localhost:18888 (no auth)
        and the forwarder injects credentials when tunneling upstream.
        """
        if not self.proxy:
            return None, None
        forwarder = _LocalProxyForwarder(self.proxy)
        await forwarder.start()
        logger.info("Local proxy forwarder started on %s", forwarder.local_url)
        return forwarder, forwarder.local_url

    @staticmethod
    def _build_url(city: str, country: str, query: str) -> str:
        """Construct the Google Maps search URL for a city/country query."""
        full_query = f"{query} in {city}, {country}"
        return f"https://www.google.com/maps/search/{quote(full_query)}"

    @staticmethod
    def _build_grid_url(lat: float, lng: float, zoom: int, query: str) -> str:
        """Construct a Google Maps search URL anchored to specific coordinates."""
        return f"https://www.google.com/maps/search/{quote(query)}/@{lat:.6f},{lng:.6f},{zoom}z"

    def _should_continue(self, businesses: List[BusinessInfo], html: str) -> bool:
        """Return False when we have enough results or hit the last page."""
        if self.max_results and len(businesses) >= self.max_results:
            return False
        if has_reached_end_of_list(html):
            return False
        return True

    async def _scrape_single_tile(
        self,
        crawler: AsyncWebCrawler,
        tile_url: str,
        seen_keys: set,
    ) -> List[BusinessInfo]:
        """
        Run Phase 1 (VirtualScrollConfig) + Phase 2 (manual JS-scroll fallback)
        for a single URL inside an already-open crawler session.

        Args:
            crawler:   Active AsyncWebCrawler context (browser already open).
            tile_url:  URL to load for this tile.
            seen_keys: Shared deduplication set, mutated in-place.

        Returns:
            List of new BusinessInfo objects found in this tile.
        """
        tile_businesses: List[BusinessInfo] = []

        # ── Phase 1: Load page with VirtualScrollConfig ───────────────────
        logger.info("Phase 1 – Loading and auto-scrolling via VirtualScrollConfig …")

        virtual_scroll = VirtualScrollConfig(
            container_selector='div[role="feed"]',
            scroll_count=self.max_scroll_steps,
            scroll_by="container_height",
            wait_after_scroll=self.scroll_wait,
        )

        # Wait for feed OR consent dialog — whichever comes first
        wait_for_feed_or_consent = """js:() => {
            if (document.querySelector('div[role="feed"]')) return true;
            const hasFeed = document.querySelector('div[role="feed"]') !== null;
            const hasConsent = ['#L2AGLb','button[jsname="b3VHJd"]','.sy4vM',
                'button[jsname="higCR"]','button[aria-label*="Accept"]']
                .some(s => document.querySelector(s));
            const hasConsentText = [...document.querySelectorAll('button')]
                .some(b => /^(accept all|i agree|agree|accept)$/i.test(b.textContent.trim()));
            if (hasConsent || hasConsentText) return true;
            return hasFeed;
        }"""

        init_cfg = CrawlerRunConfig(
            session_id=self._session_id,
            wait_for=wait_for_feed_or_consent,
            js_code=[JS_DISMISS_CONSENT, JS_CLOSE_OVERLAYS],
            virtual_scroll_config=virtual_scroll,
            magic=True,
            simulate_user=True,
            override_navigator=True,
            remove_consent_popups=True,
            cache_mode=CacheMode.BYPASS,
            page_timeout=120_000,
            delay_before_return_html=1.5,
            verbose=False,
        )

        result = await crawler.arun(url=tile_url, config=init_cfg)

        if not result.success:
            logger.warning("Tile load failed: %s", result.error_message)
            return tile_businesses  # skip this tile gracefully

        new = extract_businesses_from_html(result.html, seen_keys)
        tile_businesses.extend(new)
        logger.info("Phase 1 complete – %d businesses collected so far.", len(tile_businesses))

        # ── Phase 2: Manual JS-scroll fallback ───────────────────────────
        if not self._should_continue(tile_businesses, result.html):
            logger.info("Phase 1 collected all available results. Skipping Phase 2.")
        elif len(tile_businesses) >= self.phase2_threshold:
            logger.info(
                "Phase 1 found %d results (≥ threshold %d) – skipping Phase 2 to save proxy.",
                len(tile_businesses), self.phase2_threshold,
            )
        else:
            logger.info("Phase 2 – Manual scroll loop to catch remaining results …")
            tile_businesses = await self._manual_scroll_loop(
                crawler, tile_url, tile_businesses, seen_keys
            )

        return tile_businesses

    async def _manual_scroll_loop(
        self,
        crawler: AsyncWebCrawler,
        search_url: str,
        businesses: List[BusinessInfo],
        seen_keys: set,
    ) -> List[BusinessInfo]:
        """
        Iteratively scroll the results panel using js_only=True inside the
        existing browser session, collecting new businesses each time.
        """
        stale_streak = 0
        prev_total = len(businesses)

        for step in range(1, self.max_scroll_steps + 1):
            # Randomised wait to reduce detection risk
            delay = self.scroll_wait + random.uniform(0.3, 1.2)
            await asyncio.sleep(delay)

            scroll_cfg = CrawlerRunConfig(
                session_id=self._session_id,
                js_code=[JS_SCROLL_PANEL],
                js_only=True,           # Execute JS in existing page, no reload
                wait_for=f"js:{JS_WAIT_FOR_MORE}",
                wait_for_timeout=8_000,
                delay_before_return_html=self.scroll_wait,
                cache_mode=CacheMode.BYPASS,
                verbose=False,
            )

            result = await crawler.arun(url=search_url, config=scroll_cfg)

            if not result.success:
                err = result.error_message or ""
                logger.warning("Scroll step %d failed: %s", step, err)

                # Browser session died (closed tab/context) — attempt full reload to recover
                if "has been closed" in err or "Target closed" in err or "Session closed" in err:
                    logger.info("Browser session lost – attempting full page reload to recover …")
                    reload_cfg = CrawlerRunConfig(
                        session_id=self._session_id,
                        js_code=[JS_DISMISS_CONSENT, JS_CLOSE_OVERLAYS],
                        cache_mode=CacheMode.BYPASS,
                        page_timeout=60_000,
                        delay_before_return_html=self.scroll_wait,
                        verbose=False,
                    )
                    try:
                        recovery = await crawler.arun(url=search_url, config=reload_cfg)
                        if recovery.success:
                            logger.info("Session recovered – continuing scroll loop.")
                            new = extract_businesses_from_html(recovery.html, seen_keys)
                            businesses.extend(new)
                            prev_total = len(businesses)
                            stale_streak = 0
                            continue
                    except Exception as exc:
                        logger.warning("Recovery attempt failed: %s", exc)
                    logger.warning("Could not recover browser session – stopping scroll loop.")
                    break

                stale_streak += 1
                if stale_streak >= self.STALE_STREAK_LIMIT:
                    logger.info("Too many consecutive failures – stopping scroll loop.")
                    break
                continue

            # Parse incremental results
            new = extract_businesses_from_html(result.html, seen_keys)
            businesses.extend(new)
            current_total = len(businesses)

            logger.info(
                "Scroll %2d/%d – +%d new  (total %d)",
                step,
                self.max_scroll_steps,
                len(new),
                current_total,
            )

            # Detect stale scrolls
            if current_total == prev_total:
                stale_streak += 1
                logger.debug("No new results this scroll (streak %d/%d).", stale_streak, self.STALE_STREAK_LIMIT)
                if stale_streak >= self.STALE_STREAK_LIMIT:
                    logger.info("No new results after %d consecutive scrolls – stopping.", stale_streak)
                    break
            else:
                stale_streak = 0
                prev_total = current_total

            # Enforce max_results cap
            if self.max_results and current_total >= self.max_results:
                logger.info("Reached max_results limit (%d).", self.max_results)
                businesses = businesses[: self.max_results]
                break

            # Check for end-of-list marker
            if has_reached_end_of_list(result.html):
                logger.info("Google Maps indicates end of results.")
                break

        return businesses


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def save_to_json(businesses: List[BusinessInfo], filepath: str = "results.json") -> None:
    """Serialize results to a JSON file."""
    data = [b.to_dict() for b in businesses]
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    logger.info("Saved %d records → %s", len(businesses), filepath)


def save_to_csv(businesses: List[BusinessInfo], filepath: str = "results.csv") -> None:
    """Serialize results to a CSV file."""
    if not businesses:
        logger.warning("No records to write to CSV.")
        return

    fieldnames = list(businesses[0].to_dict().keys())

    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for biz in businesses:
            writer.writerow(biz.to_dict())

    logger.info("Saved %d records → %s", len(businesses), filepath)


# ---------------------------------------------------------------------------
# Top-level async entry points
# ---------------------------------------------------------------------------

def build_output_filename(city: str, country: str, search_query: str, ext: str) -> str:
    """
    Build an output filename that encodes the search inputs and today's date.

    Example: farmacia_guadalajara_mexico_2026-03-25.csv
    """
    def slug(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s-]", "", s)   # remove special chars
        s = re.sub(r"[\s]+", "_", s)      # spaces → underscore
        return s

    date_str = datetime.now().strftime("%Y-%m-%d")
    name = f"{slug(search_query)}_{slug(city)}_{slug(country)}_{date_str}.{ext}"
    return name


def build_grid_output_filename(search_query: str, ext: str, city: Optional[str] = None) -> str:
    """Build output filename for grid mode.

    With city:    <query>_<city>_grid_<date>.<ext>
    Without city: <query>_grid_<date>.<ext>
    """
    def slug(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s]+", "_", s)
        return s

    date_str = datetime.now().strftime("%Y-%m-%d")
    if city:
        return f"{slug(search_query)}_{slug(city)}_grid_{date_str}.{ext}"
    return f"{slug(search_query)}_grid_{date_str}.{ext}"


async def run_scraper(
    city: str,
    country: str,
    search_query: str,
    max_results: Optional[int] = None,
    headless: bool = True,
    proxy: Optional[str] = None,
    output_json: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> List[BusinessInfo]:
    """
    Convenience wrapper: scrape and save results.

    Output filenames default to  <query>_<city>_<country>_<YYYY-MM-DD>.{json,csv}
    when not specified explicitly.
    """
    if output_json is None:
        output_json = build_output_filename(city, country, search_query, "json")
    if output_csv is None:
        output_csv = build_output_filename(city, country, search_query, "csv")

    scraper = GoogleMapsScraper(
        headless=headless,
        max_results=max_results,
        proxy=proxy,
    )

    businesses = await scraper.scrape(
        city=city,
        country=country,
        search_query=search_query,
    )

    if businesses:
        save_to_json(businesses, output_json)
        save_to_csv(businesses, output_csv)
    else:
        logger.warning("No businesses found – output files not written.")

    return businesses


async def run_scraper_grid(
    query: str,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    rows: int = 8,
    cols: int = 10,
    zoom: int = 14,
    max_results: Optional[int] = None,
    headless: bool = True,
    proxy: Optional[str] = None,
    city: Optional[str] = None,
    output_json: Optional[str] = None,
    output_csv: Optional[str] = None,
    max_scroll_steps: int = 8,
    phase2_threshold: int = 3,
    tile_stale_limit: int = 6,
    tile_timeout: float = 240,
) -> List[BusinessInfo]:
    """
    Convenience wrapper for grid scraping: scrape and save results.

    Output filenames default to <query>_<city>_grid_<YYYY-MM-DD>.{json,csv}
    when city is provided, or <query>_grid_<YYYY-MM-DD>.{json,csv} otherwise.
    """
    if output_json is None:
        output_json = build_grid_output_filename(query, "json", city)
    if output_csv is None:
        output_csv = build_grid_output_filename(query, "csv", city)

    scraper = GoogleMapsScraper(
        headless=headless,
        max_results=max_results,
        proxy=proxy,
        max_scroll_steps=max_scroll_steps,
        phase2_threshold=phase2_threshold,
        tile_stale_limit=tile_stale_limit,
        tile_timeout=tile_timeout,
    )

    def _checkpoint(biz: List[BusinessInfo]) -> None:
        save_to_json(biz, output_json)
        save_to_csv(biz, output_csv)

    businesses = await scraper.scrape_grid(
        query=query,
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        rows=rows,
        cols=cols,
        zoom=zoom,
        checkpoint_fn=_checkpoint,
        checkpoint_every=10,
    )

    if businesses:
        save_to_json(businesses, output_json)
        save_to_csv(businesses, output_csv)
    else:
        logger.warning("No businesses found – output files not written.")

    return businesses


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape Google Maps business listings with crawl4ai.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Shared args ──────────────────────────────────────────────────────
    parser.add_argument("--query",       required=True, help='Search query, e.g. "reparacion automotriz"')
    parser.add_argument("--max-results", type=int, default=None,
                        help="Maximum number of businesses to collect (unlimited by default)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show the browser window (useful for debugging)")
    parser.add_argument("--proxy", default=None,
                        help='Proxy URL, e.g. "http://user:pass@host:port"')
    parser.add_argument("--output-json", default=None,
                        help="Path for JSON output (auto-generated if omitted)")
    parser.add_argument("--output-csv",  default=None,
                        help="Path for CSV output  (auto-generated if omitted)")

    # ── Single-search args (original mode) ───────────────────────────────
    parser.add_argument("--city",    default=None, help="City to search in  [single mode]")
    parser.add_argument("--country", default=None, help="Country to search in  [single mode]")

    # ── Grid-search args ─────────────────────────────────────────────────
    parser.add_argument("--grid", action="store_true",
                        help="Enable grid mode: divide the city into tiles and scrape each one")
    parser.add_argument("--preset-city", default=None,
                        choices=list(CITY_BBOXES.keys()),
                        metavar="CITY",
                        help=(
                            "Use a predefined bounding box for this city. "
                            f"Available: {', '.join(CITY_BBOXES.keys())}"
                        ))
    parser.add_argument("--bbox", nargs=4, type=float,
                        metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"),
                        help="Custom bounding box. Overrides --preset-city.")
    parser.add_argument("--rows", type=int, default=8,
                        help="Grid rows – north-to-south divisions  [grid mode]")
    parser.add_argument("--cols", type=int, default=10,
                        help="Grid columns – west-to-east divisions  [grid mode]")
    parser.add_argument("--zoom", type=int, default=14,
                        help="Google Maps zoom level (13≈5km, 14≈2.5km visible diameter)  [grid mode]")
    parser.add_argument("--max-scroll-steps", type=int, default=8,
                        help="Max scroll iterations per phase per tile (lower = less proxy usage)")
    parser.add_argument("--phase2-threshold", type=int, default=3,
                        help="Skip Phase 2 if Phase 1 already found this many new results per tile")
    parser.add_argument("--tile-stale-limit", type=int, default=6,
                        help="Stop grid after this many consecutive tiles with zero new results")
    parser.add_argument("--tile-timeout", type=float, default=240,
                        help="Max seconds per tile before skipping it (prevents overnight hangs)")

    args = parser.parse_args()

    if args.grid:
        # Resolve bounding box: --bbox takes priority over --preset-city
        if args.bbox:
            lat_min, lat_max, lon_min, lon_max = args.bbox
        elif args.preset_city:
            bb = CITY_BBOXES[args.preset_city]
            lat_min, lat_max = bb["lat_min"], bb["lat_max"]
            lon_min, lon_max = bb["lon_min"], bb["lon_max"]
        else:
            parser.error("--grid requires either --preset-city or --bbox.")

        results = asyncio.run(
            run_scraper_grid(
                query=args.query,
                lat_min=lat_min,
                lat_max=lat_max,
                lon_min=lon_min,
                lon_max=lon_max,
                rows=args.rows,
                cols=args.cols,
                zoom=args.zoom,
                max_results=args.max_results,
                headless=not args.no_headless,
                proxy=args.proxy,
                city=args.preset_city,
                output_json=args.output_json,
                output_csv=args.output_csv,
                max_scroll_steps=args.max_scroll_steps,
                phase2_threshold=args.phase2_threshold,
                tile_stale_limit=args.tile_stale_limit,
                tile_timeout=args.tile_timeout,
            )
        )
    else:
        if not args.city or not args.country:
            parser.error("Single mode requires --city and --country (or use --grid for grid mode).")

        results = asyncio.run(
            run_scraper(
                city=args.city,
                country=args.country,
                search_query=args.query,
                max_results=args.max_results,
                headless=not args.no_headless,
                proxy=args.proxy,
                output_json=args.output_json,
                output_csv=args.output_csv,
            )
        )

    print(f"\n{'='*50}")
    print(f"  Total businesses scraped: {len(results)}")
    print(f"{'='*50}")
    if results:
        print("\nSample (first 5):")
        for biz in results[:5]:
            stars = f"{biz.rating}★" if biz.rating else "no rating"
            reviews = f"{biz.reviews_count} reviews" if biz.reviews_count else "no reviews"
            print(f"  • {biz.name}  [{stars}, {reviews}]")
            if biz.address:
                print(f"    {biz.address}")
            if biz.phone:
                print(f"    {biz.phone}")
