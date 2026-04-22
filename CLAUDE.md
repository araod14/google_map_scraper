# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
make install        # create venv + install production deps
make install-dev    # create venv + install dev deps (pytest)
playwright install chromium
source venv/bin/activate
```

Manual install:
```bash
pip install crawl4ai==0.8.6 beautifulsoup4 lxml
```

## Running the scraper

**Single-city mode** (~120 results max):
```bash
python scraper.py --city "Guadalajara" --country "Mexico" --query "reparacion automotriz"
```

**Grid mode** (recommended, up to 2000+ results):
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz"
python scraper.py --grid --bbox 20.55 20.75 -103.45 -103.20 --query "taller mecanico"
```

**Debug with visible browser and small grid:**
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz" --no-headless --rows 2 --cols 2
```

**Batch mode** (multiple cities sequentially):
```bash
python run_grid_batch.py "reparacion automotriz"
make scrape-grid-batch QUERY="reparacion automotriz"
```

**Post-process: add zip codes:**
```bash
python enrich_zipcode.py input.json output.json
```

## Makefile targets

```
make scrape              CITY="..." COUNTRY="..." QUERY="..."
make scrape-grid         CITY="..." QUERY="..."
make scrape-grid-debug   CITY="..." QUERY="..."   (2×2 grid, visible browser)
make scrape-grid-batch   QUERY="..."
make test / make test-short
make clean               remove __pycache__, .pyc
make clean-results       remove all .json and .csv output files
```

## CLI flags reference

**Shared:**
```
--query TEXT          [required] search query
--max-results INT     cap total results (default: unlimited)
--no-headless         show browser window
--proxy URL           http://user:pass@host:port
--output-json PATH    override auto-generated JSON filename
--output-csv PATH     override auto-generated CSV filename
```

**Single-city mode:**
```
--city NAME           [required]
--country NAME        [required]
```

**Grid mode (--grid):**
```
--preset-city NAME         use predefined bbox from CITY_BBOXES
--bbox LAT_MIN LAT_MAX LON_MIN LON_MAX   custom bounding box
--rows INT            north-south tile divisions (default: 8)
--cols INT            west-east tile divisions (default: 10)
--zoom INT            Maps zoom level, default 14 (≈2.5 km/tile)
--max-scroll-steps INT     max scroll iterations per phase per tile (default: 8)
--phase2-threshold INT     skip Phase 2 if Phase 1 found ≥ N results (default: 3)
--tile-stale-limit INT     stop grid after N consecutive empty tiles (default: 6)
--tile-timeout FLOAT       max seconds per tile (default: 240)
```

## Architecture

**Files:**
- `scraper.py` — main scraper (1439 lines, single file)
- `run_grid_batch.py` — batch runner for multiple cities
- `enrich_zipcode.py` — post-processing: adds `zip_code` via Nominatim reverse-geocode
- `tests/test_scraper.py` — unit tests
- `conftest.py` — pytest config

**Data flow:**
1. CLI args → `run_scraper()` or `run_scraper_grid()` (async entry points)
2. These instantiate `GoogleMapsScraper` and call `.scrape()` or `.scrape_grid()`
3. Scraper loads Google Maps URLs via `crawl4ai.AsyncWebCrawler` (Playwright backend)
4. HTML snapshots are parsed: `extract_businesses_from_html()` → `parse_business_card()` → `BusinessInfo`
5. Deduplication via `BusinessInfo.dedup_key` (`google_maps_url` or `name`)
6. Output saved as JSON + CSV

**`GoogleMapsScraper` scraping strategy:**
- `STALE_STREAK_LIMIT = 3` — stops after 3 consecutive scroll steps with no new results
- Per tile, two phases:
  - **Phase 1:** `VirtualScrollConfig` (crawl4ai auto-scroll, fast path)
  - **Phase 2:** manual JS-scroll loop (`js_only=True`) — skipped if Phase 1 found ≥ `phase2_threshold` results
- Grid shares one browser session and one `seen_keys` set across all tiles
- Saves checkpoint every 10 tiles
- Logs network usage (MB) and ETA during grid runs

**HTML parsing (`parse_business_card`):**
- Targets `div[role="feed"]` → `div[role="article"]` cards
- Multiple CSS selector fallbacks + aria-label heuristics (Google Maps class names change frequently)
- `_parse_category_and_address()` uses bullet-separator heuristics: category = no digits, address = has digits + letters

**Network tracking:**
- `_LocalProxyForwarder` — TCP proxy forwarder with `.total_mb()` for proxy bandwidth usage
- `_NetworkStats` — reads `/proc/net/dev` for system-level bytes sent/received

## Output file naming

```
Single:  {query}_{city}_{country}_{YYYY-MM-DD}.json/.csv
Grid:    {query}_{city}_grid_{YYYY-MM-DD}.json/.csv
         {query}_grid_{YYYY-MM-DD}.json/.csv       (no --city given)
```

Slug rules: lowercase, spaces → underscores, special chars removed.

## Preset cities (CITY_BBOXES)

**Mexico (20):** guadalajara, monterrey, ciudad de mexico, tijuana, puebla, toluca, leon, juarez, torreon, queretaro, san luis potosi, merida, mexicali, aguascalientes, cuernavaca, saltillo, hermosillo, culiacan, chihuahua, morelia

**UK (25):** london, manchester, birmingham, glasgow, southampton, liverpool, bristol, sheffield, leeds, edinburgh, cardiff, leicester, stoke-on-trent, hull, plymouth, nottingham, bradford, belfast, portsmouth, barnsley, brighton and hove, swindon, derby, sunderland, wolverhampton

**France (20+):** paris, marseille, lyon, toulouse, nice, nantes, bordeaux, strasbourg, montpellier, lille, rennes, toulon, rouen, aix-en-provence, clermont-ferrand, saint-denis, le mans, nimes, saint-etienne, caen, nancy, orleans, argenteuil, montreuil

## Adding a new preset city

Add an entry to `CITY_BBOXES` in `scraper.py`:
```python
"leon": {"lat_min": 21.05, "lat_max": 21.20, "lon_min": -101.75, "lon_max": -101.60},
```

## Key constants & defaults

| Constant | Default | Purpose |
|---|---|---|
| `STALE_STREAK_LIMIT` | 3 | Max consecutive empty scrolls before stopping |
| `max_scroll_steps` | 8 | Max scroll iterations per phase per tile |
| `scroll_wait` | 1.5s | Wait after each scroll step |
| `phase2_threshold` | 3 | Skip Phase 2 if Phase 1 found ≥ N results |
| `tile_stale_limit` | 6 | Stop grid after N consecutive empty tiles |
| `tile_timeout` | 240s | Max time per tile |
| `rows` / `cols` | 8 / 10 | Default grid dimensions |
| `zoom` | 14 | Maps zoom (≈2.5 km diameter per tile) |
| `checkpoint_every` | 10 | Save partial results every N tiles |
| `REQUEST_DELAY` (enrich) | 1.1s | Nominatim throttle (max 1 req/s per ToS) |
