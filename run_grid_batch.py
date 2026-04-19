import subprocess
import sys

# Lista de ciudades exactas como aparecen en CITY_BBOXES de scraper.py
CITIES = [
    "belfast",
    "portsmouth",
    "barnsley",
    "brighton and hove",
    "swindon",
    "derby",
    "sunderland",
    "wolverhampton"
]

def run_batch(query):
    for city in CITIES:
        print(f"\n{'='*60}")
        print(f">>> INICIANDO GRID SCRAPING EN: {city.upper()}")
        print(f"{'='*60}")
        
        cmd = [
            "python3", "scraper.py",
            "--grid",
            "--preset-city", city,
            "--query", query,
            "--max-results", "1000"  # Opcional: ajusta según necesites
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
