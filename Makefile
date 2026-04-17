PYTHON  = venv/bin/python
PIP     = venv/bin/pip
PYTEST  = venv/bin/pytest

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: venv
venv:
	python3 -m venv venv

.PHONY: install
install: venv
	$(PIP) install -r requirements.txt
	$(PYTHON) -m playwright install chromium

.PHONY: install-dev
install-dev: venv
	$(PIP) install -r requirements-dev.txt
	$(PYTHON) -m playwright install chromium

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test:
	$(PYTEST) tests/test_scraper.py -v

.PHONY: test-short
test-short:
	$(PYTEST) tests/test_scraper.py

# ---------------------------------------------------------------------------
# Scraper – modo simple
# ---------------------------------------------------------------------------

.PHONY: scrape
scrape:
	$(PYTHON) scraper.py --city "$(CITY)" --country "$(COUNTRY)" --query "$(QUERY)" \
		$(if $(PROXY),--proxy "$(PROXY)")

# ---------------------------------------------------------------------------
# Scraper – modo grid
# ---------------------------------------------------------------------------

.PHONY: scrape-grid
scrape-grid:
	$(PYTHON) scraper.py --grid --preset-city "$(CITY)" --query "$(QUERY)" \
		$(if $(PROXY),--proxy "$(PROXY)")

.PHONY: scrape-grid-debug
scrape-grid-debug:
	$(PYTHON) scraper.py --grid --preset-city "$(CITY)" --query "$(QUERY)" \
		--no-headless --rows 2 --cols 2 \
		$(if $(PROXY),--proxy "$(PROXY)")

# ---------------------------------------------------------------------------
# Limpieza
# ---------------------------------------------------------------------------

.PHONY: clean
clean:
	find . -name "__pycache__" -type d | xargs rm -rf
	find . -name "*.pyc" -delete
	find . -name ".pytest_cache" -type d | xargs rm -rf

.PHONY: clean-results
clean-results:
	rm -f *.json *.csv
