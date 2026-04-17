# Google Maps Scraper

Scraper de negocios en Google Maps usando [crawl4ai](https://github.com/unclecode/crawl4ai) con Playwright. Extrae nombre, rating, reseñas, categoría, dirección, teléfono, sitio web y coordenadas GPS.

## Instalación

```bash
# Crea el entorno virtual e instala dependencias
make install

# Con dependencias de desarrollo (incluye pytest)
make install-dev
```

> Sin Make: `pip install -r requirements.txt && playwright install chromium`

## Makefile

| Comando | Descripción |
|---------|-------------|
| `make install` | Crea venv e instala dependencias de producción |
| `make install-dev` | Crea venv e instala dependencias + pytest |
| `make test` | Corre todos los tests con output detallado |
| `make test-short` | Corre los tests mostrando solo el resumen |
| `make scrape CITY="..." COUNTRY="..." QUERY="..."` | Scraping en modo simple |
| `make scrape-grid CITY="..." QUERY="..."` | Scraping en modo grid con ciudad predefinida |
| `make scrape-grid-debug CITY="..." QUERY="..."` | Grid debug: 2×2 tiles con browser visible |
| `make clean` | Elimina `__pycache__` y archivos `.pyc` |
| `make clean-results` | Elimina los archivos `.json` y `.csv` generados |

### Ejemplos

```bash
# Correr los tests
make test

# Scraping simple
make scrape CITY="Guadalajara" COUNTRY="Mexico" QUERY="reparacion automotriz"

# Scraping grid con ciudad predefinida
make scrape-grid CITY="guadalajara" QUERY="reparacion automotriz"

# Debug con browser visible (2×2 tiles)
make scrape-grid-debug CITY="paris" QUERY="restaurants"
```

## Modos de uso

### Modo simple

Busca en toda la ciudad con una sola consulta. Rápido pero limitado a ~120 resultados por Google Maps.

```bash
python scraper.py --city "Guadalajara" --country "Mexico" --query "reparacion automotriz"
```

```bash
python scraper.py --city "Ciudad de Mexico" --country "Mexico" --query "dentista" --max-results 50
```

### Modo grid (recomendado para ciudades grandes)

Divide la ciudad en una cuadrícula de tiles y hace scraping de cada uno por separado. Comparte la sesión del browser y deduplica automáticamente. Ideal para obtener todos los resultados posibles.

**Con ciudad predefinida:**
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz"
```

**Con bounding box personalizada:**
```bash
python scraper.py --grid --bbox 20.55 20.75 -103.45 -103.20 --query "taller mecanico"
```

**Con todos los parámetros:**
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz" \
  --zoom 14 --rows 8 --cols 10 --max-results 2000
```

**Probar con 4 tiles y browser visible (para debugging):**
```bash
python scraper.py --grid --preset-city guadalajara --query "reparacion automotriz" \
  --no-headless --rows 2 --cols 2
```

## Opciones

| Argumento | Descripción | Default |
|-----------|-------------|---------|
| `--query` | Término de búsqueda | requerido |
| `--city` | Ciudad *(solo modo simple)* | requerido en modo simple |
| `--country` | País *(solo modo simple)* | requerido en modo simple |
| `--grid` | Activar modo grid | — |
| `--preset-city` | Ciudad predefinida *(ver tabla abajo)* | — |
| `--bbox LAT_MIN LAT_MAX LON_MIN LON_MAX` | Bounding box personalizada | — |
| `--rows` | Filas de la cuadrícula (norte→sur) | `8` |
| `--cols` | Columnas de la cuadrícula (oeste→este) | `10` |
| `--zoom` | Zoom de Maps (13≈5km, 14≈2.5km por tile) | `14` |
| `--max-results` | Límite total de resultados | sin límite |
| `--no-headless` | Mostrar el browser (útil para debug) | — |
| `--output-json` | Ruta del archivo JSON | auto-generada |
| `--output-csv` | Ruta del archivo CSV | auto-generada |

## Ciudades predefinidas

### México

| `--preset-city` | Ciudad |
|-----------------|--------|
| `guadalajara` | Guadalajara |
| `monterrey` | Monterrey |
| `mexico city` | Ciudad de México |
| `ciudad de mexico` | Ciudad de México (alias) |
| `tijuana` | Tijuana |
| `puebla` | Puebla |
| `toluca` | Toluca |
| `leon` | León |
| `juarez` | Ciudad Juárez |
| `torreon` | Torreón |
| `queretaro` | Querétaro |
| `san luis potosi` | San Luis Potosí |
| `merida` | Mérida |
| `mexicali` | Mexicali |
| `aguascalientes` | Aguascalientes |
| `cuernavaca` | Cuernavaca |
| `saltillo` | Saltillo |
| `hermosillo` | Hermosillo |
| `culiacan` | Culiacán |
| `chihuahua` | Chihuahua |
| `morelia` | Morelia |

### Reino Unido

| `--preset-city` | Ciudad |
|-----------------|--------|
| `london` | Londres |
| `manchester` | Manchester |
| `birmingham` | Birmingham |
| `glasgow` | Glasgow |
| `southampton` | Southampton |
| `liverpool` | Liverpool |
| `bristol` | Bristol |
| `sheffield` | Sheffield |
| `leeds` | Leeds |
| `edinburgh` | Edimburgo |
| `cardiff` | Cardiff |
| `leicester` | Leicester |
| `stoke-on-trent` | Stoke-on-Trent |
| `hull` | Hull |
| `plymouth` | Plymouth |
| `nottingham` | Nottingham |
| `bradford` | Bradford |
| `belfast` | Belfast |
| `portsmouth` | Portsmouth |
| `barnsley` | Barnsley |
| `brighton and hove` | Brighton and Hove |
| `swindon` | Swindon |
| `derby` | Derby |
| `sunderland` | Sunderland |
| `wolverhampton` | Wolverhampton |

### Francia

| `--preset-city` | Ciudad |
|-----------------|--------|
| `paris` | París |
| `marseille` | Marsella |
| `lyon` | Lyon |
| `toulouse` | Toulouse |
| `nice` | Niza |
| `nantes` | Nantes |
| `bordeaux` | Burdeos |
| `strasbourg` | Estrasburgo |
| `montpellier` | Montpellier |
| `lille` | Lille |
| `rennes` | Rennes |
| `toulon` | Toulon |
| `rouen` | Ruán |
| `aix-en-provence` | Aix-en-Provence |
| `clermont-ferrand` | Clermont-Ferrand |
| `saint-denis` | Saint-Denis |
| `le mans` | Le Mans |
| `nimes` | Nîmes |
| `lyon-villeurbanne` | Lyon–Villeurbanne |
| `saint-etienne` | Saint-Étienne |
| `caen` | Caen |
| `nancy` | Nancy |
| `orleans` | Orléans |
| `argenteuil` | Argenteuil |
| `montreuil` | Montreuil |

Para agregar una ciudad nueva, edita el diccionario `CITY_BBOXES` en `scraper.py`:
```python
"mi ciudad": {"lat_min": 19.00, "lat_max": 19.20, "lon_min": -99.50, "lon_max": -99.30},
```

## Archivos de salida

Los archivos se generan automáticamente con la fecha actual:

| Modo | Ejemplo |
|------|---------|
| Simple | `reparacion_automotriz_guadalajara_mexico_2026-03-25.json` |
| Grid | `reparacion_automotriz_grid_2026-03-25.json` |

Cada registro contiene:

```json
{
  "name": "Taller Mecánico El Rayo",
  "rating": 4.5,
  "reviews_count": 128,
  "category": "Taller de reparación de automóviles",
  "address": "Av. Patria 1234, Zapopan, Jalisco",
  "phone": "33 1234 5678",
  "website": "https://ejemplo.com",
  "google_maps_url": "/maps/place/Taller+Mec%C3%A1nico+El+Rayo/..."
}
```

## Cuántos resultados esperar

| Modo | Resultados típicos |
|------|--------------------|
| Simple (1 búsqueda) | ~120 |
| Grid 2×2 (4 tiles) | ~200–400 |
| Grid 8×10 (80 tiles, default) | 500–2,000+ |

El modo grid deduplica automáticamente: un negocio que aparezca en dos tiles adyacentes se cuenta solo una vez.

## Tiempo de ejecución estimado

Cada tile tarda ~30–60 segundos (scroll + parsing). Para una cuadrícula 8×10:

- **80 tiles × ~45 seg** = ~60 minutos

Para búsquedas rápidas usa una cuadrícula más pequeña (`--rows 4 --cols 5`) o un zoom mayor (`--zoom 13`).

## Uso como módulo Python

```python
import asyncio
from scraper import run_scraper, run_scraper_grid

# Modo simple
results = asyncio.run(run_scraper(
    city="Guadalajara",
    country="Mexico",
    search_query="reparacion automotriz",
))

# Modo grid
results = asyncio.run(run_scraper_grid(
    query="reparacion automotriz",
    lat_min=20.55, lat_max=20.75,
    lon_min=-103.45, lon_max=-103.20,
    rows=8, cols=10, zoom=14,
))

for biz in results:
    print(biz.name, biz.address, biz.phone)
```
