.PHONY: install test vendor seed compose-up compose-down
install:
	pip install -e ".[dev]"
test:
	pytest -m "not external_llm"
vendor:
	python3 scripts/vendor_assets.py
seed:
	python scripts/seed_dev.py
compose-up:
	docker compose up -d
compose-down:
	docker compose down
