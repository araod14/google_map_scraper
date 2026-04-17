#!/usr/bin/env python3
"""
Enriquece archivos JSON/CSV del scraper con el código postal (zip_code).

Estrategia:
  1. Extrae coordenadas de google_maps_url (!3d<lat>!4d<lon>).
  2. Si no hay coords, geocodifica el campo address.
  3. Consulta Nominatim (OpenStreetMap) para obtener el CP.

Uso:
    python enrich_zipcode.py reparacion_automotriz_guadalajara_2026-03-25.json
    python enrich_zipcode.py reparacion_automotriz_guadalajara_2026-03-25.json --output enriched.json
    python enrich_zipcode.py *.json          # varios archivos a la vez
"""

import argparse
import csv
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("enrich_zipcode")

# ---------------------------------------------------------------------------
# Nominatim helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "gmaps-scraper-enricher/1.0 (local enrichment script)"

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH  = "https://nominatim.openstreetmap.org/search"
REQUEST_DELAY     = 1.1   # Nominatim TOS: max 1 req/s


def _coords_from_url(url: str) -> tuple[Optional[float], Optional[float]]:
    """
    Extrae (lat, lon) del campo google_maps_url.

    Google Maps embeds coords in the data parameter:
        !3d<lat>!4d<lon>
    Example: ...!8m2!3d20.6760073!4d-103.3734118!...
    """
    if not url:
        return None, None
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def _zipcode_from_coords(lat: float, lon: float) -> Optional[str]:
    """Reverse-geocodifica (lat, lon) y devuelve el código postal."""
    try:
        resp = SESSION.get(
            NOMINATIM_REVERSE,
            params={"lat": lat, "lon": lon, "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("address", {}).get("postcode")
    except Exception as exc:
        logger.warning("Reverse geocoding failed (%s, %s): %s", lat, lon, exc)
        return None


def _zipcode_from_address(address: str) -> Optional[str]:
    """Geocodifica una dirección de texto y devuelve el código postal."""
    if not address:
        return None
    query = address if "mexico" in address.lower() else address + ", Mexico"
    try:
        resp = SESSION.get(
            NOMINATIM_SEARCH,
            params={"q": query, "format": "json", "addressdetails": 1, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return results[0].get("address", {}).get("postcode")
    except Exception as exc:
        logger.warning("Forward geocoding failed (%s): %s", address, exc)
    return None


def get_zipcode(record: dict) -> Optional[str]:
    """
    Obtiene el CP para un registro. Primero intenta con coords de la URL,
    luego con el campo address como fallback.
    """
    lat, lon = _coords_from_url(record.get("google_maps_url", ""))
    if lat is not None:
        return _zipcode_from_coords(lat, lon)
    return _zipcode_from_address(record.get("address", ""))


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_json(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(records: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info("JSON guardado → %s", path)


def save_csv(records: list[dict], path: Path) -> None:
    if not records:
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    logger.info("CSV guardado  → %s", path)


# ---------------------------------------------------------------------------
# Main enrichment logic
# ---------------------------------------------------------------------------

def enrich_file(input_path: Path, output_path: Optional[Path] = None) -> None:
    records = load_json(input_path)
    total = len(records)
    logger.info("Procesando %d registros de %s", total, input_path.name)

    found = 0
    for i, rec in enumerate(records, 1):
        # Skip si ya tiene CP
        if rec.get("zip_code"):
            found += 1
            continue

        zip_code = get_zipcode(rec)
        rec["zip_code"] = zip_code

        if zip_code:
            found += 1
            logger.info("[%d/%d] %-40s → %s", i, total, rec.get("name", "")[:40], zip_code)
        else:
            logger.warning("[%d/%d] %-40s → sin CP", i, total, rec.get("name", "")[:40])

        time.sleep(REQUEST_DELAY)

    logger.info("CPs encontrados: %d/%d (%.0f%%)", found, total, found / total * 100 if total else 0)

    # Determinar rutas de salida
    if output_path is None:
        output_path = input_path.with_stem(input_path.stem + "_enriched")

    save_json(records, output_path)
    save_csv(records, output_path.with_suffix(".csv"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Agrega zip_code a archivos JSON del scraper usando Nominatim.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="FILE.json",
        help="Uno o más archivos JSON generados por scraper.py",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="FILE.json",
        help="Ruta de salida (solo válido con un único archivo de entrada). "
             "Por defecto: <input>_enriched.json",
    )
    args = parser.parse_args()

    if args.output and len(args.inputs) > 1:
        parser.error("--output solo se puede usar con un único archivo de entrada.")

    for input_file in args.inputs:
        output_file = Path(args.output) if args.output else None
        enrich_file(Path(input_file), output_file)
