# Convenience wrapper around the s3migrate CLI (macOS/Linux; on Windows run
# the CLI directly — see README "Windows"). `make help` lists targets.
PY       ?= ./.venv/bin/python
CLI       = $(PY) -m s3migrate
ENV_FILE ?= .env
DB       ?= registry.sqlite3
ARGS     ?=
BASE      = --env-file $(ENV_FILE) --db $(DB) $(ARGS)

# POC: the dch-scripts folder itself, under an isolated prefix
POC_SOURCE  = $(abspath ..)
POC_PREFIX  = poc/dch-scripts
POC_DB      = registry-poc.sqlite3
POC_EXCLUDE = --exclude .venv --exclude .git --exclude __pycache__ \
              --exclude .pytest_cache --exclude .env --exclude '*.sqlite3*' \
              --exclude '*.lock' --exclude 's3migrate-*.log' --exclude '*.csv'
POC_BASE    = --env-file $(ENV_FILE) --db $(POC_DB) --source $(POC_SOURCE) \
              --prefix $(POC_PREFIX) $(POC_EXCLUDE)

.PHONY: help install test scan dry-run upload resume status verify list \
        sql pg-load poc-scan poc-dry-run poc-upload poc-verify poc-clean

help:
	@echo "install      create .venv and install dependencies"
	@echo "test         run the unit + integration test suite"
	@echo "scan         discover the source into the registry"
	@echo "dry-run      scan + write the exact key mapping (uploads nothing)"
	@echo "upload       scan, then upload a batch (ARGS='--batch-files 0' for all)"
	@echo "resume       continue after any interruption"
	@echo "status       registry summary and recent failures"
	@echo "verify       re-verify destination (ARGS='--full --sample-hash 25')"
	@echo "list         query the registry (ARGS='--ext pdf --status verified')"
	@echo "sql          open the registry in sqlite3 (SELECT * FROM registry ...)"
	@echo "pg-load      mirror the registry view into Postgres (PG_DSN=...)"
	@echo "poc-*        dch-scripts proof of concept under prefix $(POC_PREFIX)"
	@echo ""
	@echo "vars: ENV_FILE=$(ENV_FILE) DB=$(DB) ARGS='$(ARGS)'"

install:
	python3 -m venv .venv
	./.venv/bin/pip install --quiet -r requirements-dev.txt

test:
	$(PY) -m pytest tests/ -q

scan:      ; $(CLI) scan $(BASE)
dry-run:   ; $(CLI) dry-run $(BASE)
upload:    ; caffeinate -i $(CLI) upload $(BASE)
resume:    ; caffeinate -i $(CLI) resume $(BASE)
status:    ; @$(CLI) status $(BASE)
verify:    ; $(CLI) verify $(BASE)
list:      ; @$(CLI) list-files $(BASE)

sql:
	sqlite3 $(DB)

pg-load:
	@test -n "$(PG_DSN)" || { echo "usage: make pg-load PG_DSN=postgres://user:pass@host/db"; exit 1; }
	sqlite3 -header -csv $(DB) "SELECT * FROM registry;" > registry-export.csv
	psql "$(PG_DSN)" -c "CREATE TABLE IF NOT EXISTS s3_registry ( \
	    id bigint PRIMARY KEY, relpath text NOT NULL, filename text, \
	    extension text, size bigint, sha256 text, s3_bucket text, \
	    s3_key text, s3_uri text, status text, verified_at timestamptz, \
	    directory text, source_root text);"
	psql "$(PG_DSN)" -c "TRUNCATE s3_registry;" \
	     -c "\copy s3_registry FROM 'registry-export.csv' CSV HEADER"
	psql "$(PG_DSN)" -c "SELECT count(*) AS registry_rows FROM s3_registry;"

poc-scan:    ; $(CLI) scan $(POC_BASE)
poc-dry-run: ; $(CLI) dry-run $(POC_BASE) --manifest poc-manifest.csv
poc-upload:  ; $(CLI) upload $(POC_BASE) $(ARGS)
poc-verify:  ; $(CLI) verify $(POC_BASE) --full --sample-hash 5 --out poc-exceptions.csv
poc-clean:
	. ./$(ENV_FILE) 2>/dev/null; \
	AWS_ACCESS_KEY_ID=$$STORAGE_ACCESS_KEY \
	AWS_SECRET_ACCESS_KEY=$$STORAGE_SECRET_KEY \
	AWS_DEFAULT_REGION=$$STORAGE_REGION \
	aws s3 rm s3://$$STORAGE_BUCKET_NAME/$(POC_PREFIX)/ --recursive
	rm -f $(POC_DB)* poc-manifest.csv poc-exceptions.csv
