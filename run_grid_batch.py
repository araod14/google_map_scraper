import glob
import re
import subprocess
import sys

# Lista de ciudades exactas como aparecen en CITY_BBOXES de scraper.py
# Completadas (2026-04-27): london, manchester, birmingham, glasgow, southampton,
#                           liverpool, bristol, sheffield, leeds
CITIES = [
    "edinburgh",
    "cardiff",
    "leicester",
    "stoke-on-trent",
    "hull",
    "plymouth",
    "nottingham",
    "bradford",
    "belfast",
    "portsmouth",
    "barnsley",
    "brighton and hove",
    "swindon",
    "derby",
    "sunderland",
    "wolverhampton",
]


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def already_done(query, city):
    q = slugify(query)
    c = slugify(city)
    return bool(glob.glob(f"{q}_{c}_grid_*.json"))


def run_batch(query):
    skipped = []
    pending = [c for c in CITIES if not already_done(query, c)]

    if skipped_cities := [c for c in CITIES if already_done(query, c)]:
        print(f"Saltando {len(skipped_cities)} ciudad(es) ya completadas: {', '.join(skipped_cities)}")

    print(f"Ciudades pendientes ({len(pending)}): {', '.join(pending)}\n")

    for city in pending:
        print(f"\n{'='*60}")
        print(f">>> INICIANDO GRID SCRAPING EN: {city.upper()}")
        print(f"{'='*60}")

        cmd = [
            "python3", "scraper.py",
            "--grid",
            "--preset-city", city,
            "--query", query,
            "--max-results", "1000",
        ]

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Error procesando {city}: {e}")
            continue


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 run_grid_batch.py 'tu busqueda'")
        sys.exit(1)

    query = sys.argv[1]
    run_batch(query)
