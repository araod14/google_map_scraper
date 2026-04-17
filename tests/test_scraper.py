"""
Tests for scraper.py — covers all pure-Python functions (no browser required).

Run with:
    pytest tests/test_scraper.py -v
"""

import csv
import io
import json
import tempfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper import (
    CITY_BBOXES,
    BusinessInfo,
    _parse_category_and_address,
    _parse_coords_from_url,
    _parse_phone,
    _parse_rating,
    _parse_reviews_count,
    _parse_website,
    build_grid_output_filename,
    build_output_filename,
    extract_businesses_from_html,
    generate_grid_cells,
    has_reached_end_of_list,
    parse_business_card,
    save_to_csv,
    save_to_json,
    GoogleMapsScraper,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card(html: str):
    """Parse an HTML snippet and return the first BS4 element."""
    return BeautifulSoup(html, "lxml").find()


def _feed(cards_html: str) -> str:
    """Wrap card HTML snippets inside a realistic feed container."""
    return f'<div role="feed">{cards_html}</div>'


def _make_card(
    name="Taller El Güero",
    rating=4.5,
    reviews=320,
    category="Auto repair shop",
    address="Av. Vallarta 1234, Guadalajara",
    phone="+52 33 1234 5678",
    website="https://example.com",
    maps_url="/maps/place/Taller+El+G%C3%BCero/@20.6597,-103.3496,17z",
) -> str:
    """Return a minimal but realistic-looking Google Maps card HTML."""
    return f"""
    <div role="article">
      <a href="{maps_url}" aria-label="{name}">
        <span class="fontHeadlineSmall">{name}</span>
      </a>
      <span aria-label="{rating} stars">{rating}</span>
      <span aria-label="{reviews} reviews">({reviews})</span>
      <div class="W4Efsd">{category}·{address}</div>
      <span>{phone}</span>
      <a href="{website}" data-value="Website">Website</a>
    </div>
    """


# ===========================================================================
# _parse_coords_from_url
# ===========================================================================

class TestParseCoordsFromUrl:

    def test_at_pattern(self):
        url = "/maps/place/Foo/@20.6597,-103.3496,17z"
        lat, lng = _parse_coords_from_url(url)
        assert lat == pytest.approx(20.6597)
        assert lng == pytest.approx(-103.3496)

    def test_data_pattern_fallback(self):
        url = "/maps/place/Foo/data=!3d48.8566!4d2.3522"
        lat, lng = _parse_coords_from_url(url)
        assert lat == pytest.approx(48.8566)
        assert lng == pytest.approx(2.3522)

    def test_negative_coordinates(self):
        url = "/maps/place/Bar/@-33.8688,151.2093,15z"
        lat, lng = _parse_coords_from_url(url)
        assert lat == pytest.approx(-33.8688)
        assert lng == pytest.approx(151.2093)

    def test_no_coords_returns_none(self):
        lat, lng = _parse_coords_from_url("https://www.google.com/maps/search/coffee")
        assert lat is None
        assert lng is None

    def test_at_pattern_takes_priority_over_data(self):
        url = "/maps/place/Foo/@20.6597,-103.3496,17z/data=!3d99.0!4d99.0"
        lat, lng = _parse_coords_from_url(url)
        assert lat == pytest.approx(20.6597)
        assert lng == pytest.approx(-103.3496)


# ===========================================================================
# _parse_rating
# ===========================================================================

class TestParseRating:

    def test_aria_label(self):
        el = _card('<div><span aria-label="4.3 stars">4.3</span></div>')
        assert _parse_rating(el) == pytest.approx(4.3)

    def test_aria_label_case_insensitive(self):
        el = _card('<div><span aria-label="3.8 Stars">3.8</span></div>')
        assert _parse_rating(el) == pytest.approx(3.8)

    def test_class_mw4etd(self):
        el = _card('<div><span class="MW4etd">4,7</span></div>')
        assert _parse_rating(el) == pytest.approx(4.7)

    def test_no_rating_returns_none(self):
        el = _card('<div><span>No rating here</span></div>')
        assert _parse_rating(el) is None


# ===========================================================================
# _parse_reviews_count
# ===========================================================================

class TestParseReviewsCount:

    def test_aria_label(self):
        el = _card('<div><span aria-label="1,234 reviews"></span></div>')
        assert _parse_reviews_count(el) == 1234

    def test_aria_label_singular(self):
        el = _card('<div><span aria-label="1 review"></span></div>')
        assert _parse_reviews_count(el) == 1

    def test_class_uy7f9(self):
        el = _card('<div><span class="UY7F9">(320)</span></div>')
        assert _parse_reviews_count(el) == 320

    def test_no_reviews_returns_none(self):
        el = _card('<div><span>nothing</span></div>')
        assert _parse_reviews_count(el) is None


# ===========================================================================
# _parse_category_and_address
# ===========================================================================

class TestParseCategoryAndAddress:

    def test_typical_card(self):
        el = _card(
            '<div>'
            '<div class="W4Efsd">Auto repair shop · Av. Vallarta 1234, Guadalajara</div>'
            '</div>'
        )
        category, address = _parse_category_and_address(el)
        assert category == "Auto repair shop"
        assert "1234" in address

    def test_only_category_no_address(self):
        el = _card('<div><div class="W4Efsd">Coffee shop</div></div>')
        category, address = _parse_category_and_address(el)
        assert category == "Coffee shop"
        assert address is None

    def test_empty_element(self):
        el = _card('<div></div>')
        category, address = _parse_category_and_address(el)
        assert category is None
        assert address is None

    def test_skips_pure_numeric_fragments(self):
        el = _card('<div><div class="W4Efsd">4.5 · 2.3 km · Bakery · Calle 5 No. 10</div></div>')
        category, address = _parse_category_and_address(el)
        assert category == "Bakery"
        assert "10" in address


# ===========================================================================
# _parse_phone
# ===========================================================================

class TestParsePhone:

    def test_plain_text_phone(self):
        el = _card('<div><span>+52 33 1234 5678</span></div>')
        assert _parse_phone(el) == "+52 33 1234 5678"

    def test_aria_label_phone(self):
        el = _card('<div><span aria-label="phone: 555-1234"></span></div>')
        assert _parse_phone(el) == "555-1234"

    def test_no_phone_returns_none(self):
        el = _card('<div><span>no numbers here</span></div>')
        assert _parse_phone(el) is None


# ===========================================================================
# _parse_website
# ===========================================================================

class TestParseWebsite:

    def test_data_value_website(self):
        el = _card('<div><a href="https://example.com" data-value="Website">Website</a></div>')
        assert _parse_website(el) == "https://example.com"

    def test_outbound_non_google_link(self):
        el = _card('<div><a href="https://mybusiness.mx">Visit us</a></div>')
        assert _parse_website(el) == "https://mybusiness.mx"

    def test_google_link_ignored(self):
        el = _card('<div><a href="https://www.google.com/something">Google link</a></div>')
        assert _parse_website(el) is None

    def test_no_website_returns_none(self):
        el = _card('<div><span>no links</span></div>')
        assert _parse_website(el) is None


# ===========================================================================
# parse_business_card
# ===========================================================================

class TestParseBusinessCard:

    def test_full_card(self):
        soup = BeautifulSoup(_make_card(), "lxml")
        card = soup.find("div", role="article")
        biz = parse_business_card(card)

        assert biz is not None
        assert biz.name == "Taller El Güero"
        assert biz.rating == pytest.approx(4.5)
        assert biz.reviews_count == 320
        assert biz.phone == "+52 33 1234 5678"
        assert biz.website == "https://example.com"
        assert biz.latitude == pytest.approx(20.6597)
        assert biz.longitude == pytest.approx(-103.3496)

    def test_card_without_name_returns_none(self):
        html = '<div role="article"><span>no name here</span></div>'
        card = BeautifulSoup(html, "lxml").find("div", role="article")
        assert parse_business_card(card) is None

    def test_card_without_coords(self):
        card_html = _make_card(maps_url="/maps/place/SomeBusiness/")
        soup = BeautifulSoup(card_html, "lxml")
        card = soup.find("div", role="article")
        biz = parse_business_card(card)
        assert biz is not None
        assert biz.latitude is None
        assert biz.longitude is None

    def test_url_query_params_stripped(self):
        card_html = _make_card(maps_url="/maps/place/Foo/@20.65,-103.34,17z?authuser=0")
        soup = BeautifulSoup(card_html, "lxml")
        card = soup.find("div", role="article")
        biz = parse_business_card(card)
        assert "?" not in (biz.google_maps_url or "")

    def test_name_fallback_to_aria_label(self):
        html = """
        <div role="article">
          <a href="/maps/place/FallbackBiz/@1.0,2.0,17z" aria-label="Fallback Name"></a>
        </div>
        """
        card = BeautifulSoup(html, "lxml").find("div", role="article")
        biz = parse_business_card(card)
        assert biz is not None
        assert biz.name == "Fallback Name"


# ===========================================================================
# extract_businesses_from_html
# ===========================================================================

class TestExtractBusinessesFromHtml:

    def test_extracts_multiple_businesses(self):
        card_a = _make_card(name="Biz A", maps_url="/maps/place/BizA/@20.65,-103.34,17z")
        card_b = _make_card(name="Biz B", maps_url="/maps/place/BizB/@20.66,-103.35,17z")
        feed_html = _feed(card_a + card_b)
        results = extract_businesses_from_html(feed_html, set())
        names = {b.name for b in results}
        assert "Biz A" in names
        assert "Biz B" in names

    def test_deduplication(self):
        card = _make_card(name="Biz A")
        feed_html = _feed(card + card)  # same card twice
        results = extract_businesses_from_html(feed_html, set())
        assert len(results) == 1

    def test_seen_keys_respected(self):
        card = _make_card(name="Biz A", maps_url="/maps/place/BizA/@20.6,-103.3,17z")
        feed_html = _feed(card)
        seen = {"/maps/place/BizA/@20.6,-103.3,17z"}
        results = extract_businesses_from_html(feed_html, seen)
        assert results == []

    def test_no_feed_returns_empty(self):
        html = "<html><body><div>no feed here</div></body></html>"
        results = extract_businesses_from_html(html, set())
        assert results == []

    def test_seen_keys_mutated(self):
        feed_html = _feed(_make_card(name="Biz A"))
        seen: set = set()
        extract_businesses_from_html(feed_html, seen)
        assert len(seen) == 1


# ===========================================================================
# has_reached_end_of_list
# ===========================================================================

class TestHasReachedEndOfList:

    def test_detects_end_marker(self):
        html = "<html><body>You've reached the end of the list.</body></html>"
        assert has_reached_end_of_list(html) is True

    def test_partial_marker(self):
        html = "<html><body>end of the list</body></html>"
        assert has_reached_end_of_list(html) is True

    def test_no_marker(self):
        html = "<html><body>Some results here</body></html>"
        assert has_reached_end_of_list(html) is False

    def test_no_more_results_marker(self):
        html = "<html><body>no more results</body></html>"
        assert has_reached_end_of_list(html) is True


# ===========================================================================
# generate_grid_cells
# ===========================================================================

class TestGenerateGridCells:

    def test_cell_count(self):
        cells = list(generate_grid_cells(20.0, 21.0, -104.0, -103.0, rows=3, cols=4))
        assert len(cells) == 12

    def test_centers_within_bbox(self):
        lat_min, lat_max = 20.0, 21.0
        lon_min, lon_max = -104.0, -103.0
        for lat, lng in generate_grid_cells(lat_min, lat_max, lon_min, lon_max, rows=4, cols=4):
            assert lat_min <= lat <= lat_max
            assert lon_min <= lng <= lon_max

    def test_single_cell(self):
        cells = list(generate_grid_cells(20.0, 21.0, -104.0, -103.0, rows=1, cols=1))
        assert len(cells) == 1
        lat, lng = cells[0]
        assert lat == pytest.approx(20.5)
        assert lng == pytest.approx(-103.5)

    def test_row_major_order(self):
        """First cell should be top-left (highest lat, lowest lng)."""
        cells = list(generate_grid_cells(20.0, 21.0, -104.0, -103.0, rows=2, cols=2))
        lats = [c[0] for c in cells]
        # First two cells (top row) should have higher lat than last two (bottom row)
        assert lats[0] > lats[2]
        assert lats[1] > lats[3]


# ===========================================================================
# GoogleMapsScraper static helpers
# ===========================================================================

class TestGoogleMapsScraperHelpers:

    def test_build_url_encodes_query(self):
        url = GoogleMapsScraper._build_url("Guadalajara", "Mexico", "reparacion automotriz")
        assert "reparacion%20automotriz" in url or "reparacion+automotriz" in url
        assert "Guadalajara" in url or "guadalajara" in url.lower()

    def test_build_grid_url_contains_coords(self):
        url = GoogleMapsScraper._build_grid_url(20.6597, -103.3496, 14, "talleres")
        assert "20.659700" in url
        assert "-103.349600" in url
        assert "14z" in url

    def test_should_continue_max_results(self):
        scraper = GoogleMapsScraper(max_results=5)
        businesses = [BusinessInfo(name=f"Biz {i}") for i in range(5)]
        html = "<html><body>results</body></html>"
        assert scraper._should_continue(businesses, html) is False

    def test_should_continue_end_of_list(self):
        scraper = GoogleMapsScraper()
        businesses = [BusinessInfo(name="Biz 1")]
        html = "<html><body>You've reached the end of the list.</body></html>"
        assert scraper._should_continue(businesses, html) is False

    def test_should_continue_normal(self):
        scraper = GoogleMapsScraper(max_results=100)
        businesses = [BusinessInfo(name="Biz 1")]
        html = "<html><body>some results</body></html>"
        assert scraper._should_continue(businesses, html) is True


# ===========================================================================
# BusinessInfo
# ===========================================================================

class TestBusinessInfo:

    def test_dedup_key_prefers_url(self):
        biz = BusinessInfo(name="Foo", google_maps_url="/maps/place/Foo")
        assert biz.dedup_key == "/maps/place/Foo"

    def test_dedup_key_falls_back_to_name(self):
        biz = BusinessInfo(name="Foo", google_maps_url=None)
        assert biz.dedup_key == "Foo"

    def test_dedup_key_empty_when_no_data(self):
        biz = BusinessInfo()
        assert biz.dedup_key == ""

    def test_to_dict_includes_coords(self):
        biz = BusinessInfo(name="Foo", latitude=20.65, longitude=-103.34)
        d = biz.to_dict()
        assert d["latitude"] == 20.65
        assert d["longitude"] == -103.34

    def test_to_dict_all_fields_present(self):
        biz = BusinessInfo()
        keys = biz.to_dict().keys()
        for field in ["name", "rating", "reviews_count", "category",
                      "address", "phone", "website", "google_maps_url",
                      "latitude", "longitude"]:
            assert field in keys


# ===========================================================================
# save_to_json / save_to_csv
# ===========================================================================

class TestSaveOutputs:

    def _sample_businesses(self):
        return [
            BusinessInfo(
                name="Taller A",
                rating=4.5,
                reviews_count=100,
                category="Auto repair",
                address="Calle 1 No. 10",
                phone="+52 33 0000 0000",
                website="https://a.com",
                google_maps_url="/maps/place/A",
                latitude=20.65,
                longitude=-103.34,
            ),
            BusinessInfo(
                name="Taller B",
                rating=3.8,
                reviews_count=50,
                latitude=None,
                longitude=None,
            ),
        ]

    def test_save_to_json_roundtrip(self, tmp_path):
        filepath = str(tmp_path / "out.json")
        businesses = self._sample_businesses()
        save_to_json(businesses, filepath)

        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)

        assert len(data) == 2
        assert data[0]["name"] == "Taller A"
        assert data[0]["latitude"] == 20.65
        assert data[0]["longitude"] == -103.34
        assert data[1]["latitude"] is None

    def test_save_to_csv_has_coord_columns(self, tmp_path):
        filepath = str(tmp_path / "out.csv")
        businesses = self._sample_businesses()
        save_to_csv(businesses, filepath)

        with open(filepath, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        assert "latitude" in rows[0]
        assert "longitude" in rows[0]
        assert rows[0]["latitude"] == "20.65"
        assert rows[0]["longitude"] == "-103.34"
        assert rows[1]["latitude"] == ""

    def test_save_to_csv_empty_list(self, tmp_path, caplog):
        filepath = str(tmp_path / "empty.csv")
        save_to_csv([], filepath)
        assert not Path(filepath).exists()

    def test_save_to_json_unicode(self, tmp_path):
        filepath = str(tmp_path / "unicode.json")
        businesses = [BusinessInfo(name="Taller Ñoño")]
        save_to_json(businesses, filepath)
        with open(filepath, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data[0]["name"] == "Taller Ñoño"


# ===========================================================================
# build_output_filename / build_grid_output_filename
# ===========================================================================

class TestBuildOutputFilenames:

    def test_simple_filename_format(self):
        name = build_output_filename("Guadalajara", "Mexico", "reparacion automotriz", "csv")
        assert name.endswith(".csv")
        assert "guadalajara" in name
        assert "mexico" in name
        assert "reparacion_automotriz" in name

    def test_grid_filename_format(self):
        name = build_grid_output_filename("taller mecanico", "json")
        assert name.endswith(".json")
        assert "taller_mecanico" in name
        assert "grid" in name

    def test_grid_filename_with_city(self):
        name = build_grid_output_filename("self storage facility", "csv", city="london")
        assert name.endswith(".csv")
        assert "self_storage_facility" in name
        assert "london" in name
        assert "grid" in name

    def test_grid_filename_without_city(self):
        name = build_grid_output_filename("self storage facility", "csv")
        assert "london" not in name
        assert "grid" in name

    def test_special_chars_removed(self):
        name = build_output_filename("São Paulo", "Brasil", "café & bar", "csv")
        assert "&" not in name
        assert " " not in name


# ===========================================================================
# CITY_BBOXES
# ===========================================================================

class TestCityBboxes:

    def test_all_cities_have_required_keys(self):
        required = {"lat_min", "lat_max", "lon_min", "lon_max"}
        for city, bbox in CITY_BBOXES.items():
            assert required == bbox.keys(), f"Missing keys for '{city}'"

    def test_lat_min_less_than_lat_max(self):
        for city, bbox in CITY_BBOXES.items():
            assert bbox["lat_min"] < bbox["lat_max"], f"lat_min >= lat_max for '{city}'"

    def test_lon_min_less_than_lon_max(self):
        for city, bbox in CITY_BBOXES.items():
            assert bbox["lon_min"] < bbox["lon_max"], f"lon_min >= lon_max for '{city}'"

    def test_mexican_cities_present(self):
        for city in ["guadalajara", "monterrey", "mexico city", "tijuana", "puebla"]:
            assert city in CITY_BBOXES

    def test_uk_cities_present(self):
        for city in ["london", "manchester", "birmingham", "glasgow", "liverpool"]:
            assert city in CITY_BBOXES

    def test_french_cities_present(self):
        for city in ["paris", "marseille", "lyon", "toulouse", "nice"]:
            assert city in CITY_BBOXES

    def test_lat_values_are_valid_degrees(self):
        for city, bbox in CITY_BBOXES.items():
            assert -90 <= bbox["lat_min"] <= 90, f"Invalid lat_min for '{city}'"
            assert -90 <= bbox["lat_max"] <= 90, f"Invalid lat_max for '{city}'"

    def test_lon_values_are_valid_degrees(self):
        for city, bbox in CITY_BBOXES.items():
            assert -180 <= bbox["lon_min"] <= 180, f"Invalid lon_min for '{city}'"
            assert -180 <= bbox["lon_max"] <= 180, f"Invalid lon_max for '{city}'"
