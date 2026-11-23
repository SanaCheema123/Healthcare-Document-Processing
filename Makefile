.PHONY: install test lint run eval docker sample

install:
	pip install -r requirements-dev.txt

test:
	pytest

lint:
	ruff check app tests scripts

run:
	uvicorn app.main:app --reload --port 8000

eval:
	python scripts/evaluate.py --out eval/report_$${MEDX_LLM_PROVIDER:-rules}.json

sample:
	python scripts/make_scanned_sample.py samples/claim_cms1500.txt samples/claim_scanned.png

docker:
	docker compose up --build
