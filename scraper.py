#!/usr/bin/env python3
"""
Google Maps Scraper using crawl4ai (v0.8.6)

Scrapes business listings from Google Maps based on city, country, and
search query. Uses Playwright under the hood via crawl4ai's AsyncWebCrawler
with VirtualScrollConfig for infinite-scroll handling.

Usage:
    python scraper.py --city "Guadalajara" --country "Mexico" --query "auto repair"
    python scraper.py --city "NYC" --country "USA" --query "coffee shop" --max-results 50
"""

import asyncio
import json
from datetime import datetime
import csv
import logging
import re
import random
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
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
    const selectors = [
        'button[aria-label*="Accept"]',
        'button[aria-label*="Agree"]',
        '#L2AGLb',            // "I agree" on google.com consent page
        'button[jsname="b3VHJd"]',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) { btn.click(); return 'consent_dismissed'; }
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

        # --- Google Maps URL ------------------------------------------------
        anchor = card_element.find("a", href=re.compile(r"/maps/place/"))
        if anchor:
            href = anchor.get("href", "")
            # Keep only the canonical place path, drop query params
            biz.google_maps_url = re.sub(r"\?.*$", "", href) if href else None

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
    """

    # How many consecutive scroll steps with zero new results before giving up
    STALE_STREAK_LIMIT = 3

    def __init__(
        self,
        headless: bool = True,
        max_results: Optional[int] = None,
        max_scroll_steps: int = 25,
        scroll_wait: float = 2.0,
    ):
        """
        Args:
            headless:         Run browser without a visible window.
            max_results:      Stop after collecting this many businesses (None = unlimited).
            max_scroll_steps: Maximum number of manual scroll iterations as fallback.
            scroll_wait:      Base seconds to wait after each scroll for content to load.
        """
        self.headless = headless
        self.max_results = max_results
        self.max_scroll_steps = max_scroll_steps
        self.scroll_wait = scroll_wait
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

        browser_cfg = BrowserConfig(
            headless=self.headless,
            verbose=False,
            viewport_width=1280,
            viewport_height=900,
            enable_stealth=True,    # Reduces bot-detection fingerprinting
        )

        businesses: List[BusinessInfo] = []
        seen_keys: set = set()

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            # ── Phase 1: Initial page load ─────────────────────────────────
            logger.info("Phase 1 – Loading page and auto-scrolling via VirtualScrollConfig …")

            virtual_scroll = VirtualScrollConfig(
                container_selector='div[role="feed"]',
                scroll_count=self.max_scroll_steps,
                scroll_by="container_height",
                wait_after_scroll=self.scroll_wait,
            )

            init_cfg = CrawlerRunConfig(
                session_id=self._session_id,
                wait_for='css:div[role="feed"]',
                js_code=[JS_DISMISS_CONSENT, JS_CLOSE_OVERLAYS],
                virtual_scroll_config=virtual_scroll,
                magic=True,
                simulate_user=True,
                override_navigator=True,
                remove_consent_popups=True,
                cache_mode=CacheMode.BYPASS,   # Always fetch fresh
                page_timeout=60_000,
                delay_before_return_html=1.5,
                verbose=False,
            )

            result = await crawler.arun(url=search_url, config=init_cfg)

            if not result.success:
                logger.error("Failed to load page: %s", result.error_message)
                return businesses

            # Parse the snapshot returned after auto-scrolling
            new = extract_businesses_from_html(result.html, seen_keys)
            businesses.extend(new)
            logger.info("Phase 1 complete – %d businesses collected so far.", len(businesses))

            # ── Phase 2: Manual JS-scroll fallback loop ────────────────────
            # If Phase 1 already reached the end or the limit, skip.
            if not self._should_continue(businesses, result.html):
                logger.info("Phase 1 collected all available results. Skipping Phase 2.")
            else:
                logger.info("Phase 2 – Manual scroll loop to catch remaining results …")
                businesses = await self._manual_scroll_loop(
                    crawler, search_url, businesses, seen_keys
                )

        logger.info("=== Scraping complete – %d businesses collected ===", len(businesses))
        return businesses

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_url(city: str, country: str, query: str) -> str:
        """Construct the Google Maps search URL."""
        full_query = f"{query} in {city}, {country}"
        return f"https://www.google.com/maps/search/{quote(full_query)}"

    def _should_continue(self, businesses: List[BusinessInfo], html: str) -> bool:
        """Return False when we have enough results or hit the last page."""
        if self.max_results and len(businesses) >= self.max_results:
            return False
        if has_reached_end_of_list(html):
            return False
        return True

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
                logger.warning("Scroll step %d failed: %s", step, result.error_message)
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
# Top-level async entry point
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


async def run_scraper(
    city: str,
    country: str,
    search_query: str,
    max_results: Optional[int] = None,
    headless: bool = True,
    output_json: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> List[BusinessInfo]:
    """
    Convenience wrapper: scrape and save results.

    Output filenames default to  <query>_<city>_<country>_<YYYY-MM-DD>.{json,csv}
    when not specified explicitly.

    Args:
        city:          City name (e.g. "Guadalajara").
        country:       Country name (e.g. "Mexico").
        search_query:  What to look for (e.g. "auto repair").
        max_results:   Cap on total results; None means no cap.
        headless:      Whether to run the browser in headless mode.
        output_json:   Path for the JSON output file (auto-generated if None).
        output_csv:    Path for the CSV output file (auto-generated if None).

    Returns:
        List of BusinessInfo objects that were collected and saved.
    """
    if output_json is None:
        output_json = build_output_filename(city, country, search_query, "json")
    if output_csv is None:
        output_csv = build_output_filename(city, country, search_query, "csv")

    scraper = GoogleMapsScraper(
        headless=headless,
        max_results=max_results,
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape Google Maps business listings with crawl4ai.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--city",        required=True, help="City to search in")
    parser.add_argument("--country",     required=True, help="Country to search in")
    parser.add_argument("--query",       required=True, help='Search query, e.g. "auto repair"')
    parser.add_argument("--max-results", type=int, default=None,
                        help="Maximum number of businesses to collect (unlimited by default)")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show the browser window (useful for debugging)")
    parser.add_argument("--output-json", default=None,
                        help="Path for JSON output (default: <query>_<city>_<country>_<date>.json)")
    parser.add_argument("--output-csv",  default=None,
                        help="Path for CSV output  (default: <query>_<city>_<country>_<date>.csv)")

    args = parser.parse_args()

    results = asyncio.run(
        run_scraper(
            city=args.city,
            country=args.country,
            search_query=args.query,
            max_results=args.max_results,
            headless=not args.no_headless,
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
