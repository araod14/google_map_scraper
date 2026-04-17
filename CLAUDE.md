# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install crawl4ai beautifulsoup4 lxml
playwright install chromium
source venv/bin/activate
```

## Running the scraper

**Simple mode** (single city search, ~120 results max):
```bash
python scraper.py --city "Guadalajara" --country "Mexico" --query "reparacion automotriz"
```

**Grid mode** (recommended for full city coverage, up to 2000+ results):
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz"
python scraper.py --grid --bbox 20.55 20.75 -103.45 -103.20 --query "taller mecanico"
```

**Debug with visible browser and small grid:**
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz" --no-headless --rows 2 --cols 2
```

## Architecture

Everything lives in a single file: `scraper.py`.

**Data flow:**
1. CLI args → `run_scraper()` or `run_scraper_grid()` (async entry points at the bottom of the file)
2. These instantiate `GoogleMapsScraper` and call `.scrape()` or `.scrape_grid()`
3. Scraper loads Google Maps URLs via `crawl4ai.AsyncWebCrawler` with Playwright
4. HTML snapshots are parsed by `extract_businesses_from_html()` → `parse_business_card()` → `BusinessInfo` dataclass
5. Results are deduplicated via `BusinessInfo.dedup_key` (uses `google_maps_url` or `name`)
6. Output saved as JSON + CSV with auto-generated filenames

**`GoogleMapsScraper` scraping strategy:**
- Uses `VirtualScrollConfig` (crawl4ai's built-in auto-scroll) as the fast path
- Falls back to a manual JS-scroll loop (`js_only=True` on the existing session) if needed
- Stops scrolling when `has_reached_end_of_list()` returns True or `STALE_STREAK_LIMIT` (3) consecutive steps yield no new results

**Grid mode:**
- `generate_grid_cells()` divides a bounding box into `rows×cols` tiles, yielding `(center_lat, center_lng)` for each
- All tiles share one browser session and one `seen_keys` set for deduplication across tiles
- `CITY_BBOXES` dict holds predefined bounding boxes for 21 Mexican cities

**HTML parsing** (`parse_business_card`):
- Targets `div[role="feed"]` → `div[role="article"]` cards
- Uses multiple CSS selector fallbacks and aria-label heuristics since Google Maps class names change frequently
- `_parse_category_and_address()` uses bullet-separator heuristics to distinguish category (no digits) from address (has digits + text letters)

## Adding a new preset city

Add an entry to `CITY_BBOXES` in `scraper.py`:
```python
"leon": {"lat_min": 21.05, "lat_max": 21.20, "lon_min": -101.75, "lon_max": -101.60},
```

## Output files

Auto-named files in the project root:
- Simple: `{query}_{city}_{country}_{date}.json/.csv`
- Grid: `{query}_grid_{date}.json/.csv`
