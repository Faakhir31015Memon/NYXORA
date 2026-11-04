# NYXORA - zero-budget local development. Everything here runs free on one
# machine; nothing contacts a paid API unless you explicitly set a key.
#
# On Windows run these through Git Bash. The venv layout is detected so the
# same targets work in both POSIX and Windows shells.

API     := apps/api
VENV    := $(API)/.venv
ifeq ($(OS),Windows_NT)
  VPY    := $(VENV)/Scripts/python.exe
  BOOT   := python
else
  VPY    := $(VENV)/bin/python
  BOOT   := python3
endif

.PHONY: help venv install dev test lint type fmt check db-reset clean

help:
	@echo "make venv     - create the API virtualenv"
	@echo "make install  - install pinned dependencies"
	@echo "make dev      - run the API on 127.0.0.1:8000"
	@echo "make test     - pytest"
	@echo "make lint     - ruff check"
	@echo "make type     - mypy (strict)"
	@echo "make fmt      - ruff format + autofix"
	@echo "make check    - lint + type + test, the gate CI runs"
	@echo "make db-reset - drop and rebuild the local dev database"

venv:
	$(BOOT) -m venv $(VENV)

install: venv
	$(VPY) -m pip install -r $(API)/requirements.txt

dev:
	cd $(API) && $(VPY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

test:
	cd $(API) && $(VPY) -m pytest -q

lint:
	cd $(API) && $(VPY) -m ruff check .

type:
	cd $(API) && $(VPY) -m mypy .

fmt:
	cd $(API) && $(VPY) -m ruff format . && $(VPY) -m ruff check . --fix

check: lint type test

db-reset:
	cd $(API) && $(VPY) scripts/reset_dev_db.py

clean:
	cd $(API) && $(VPY) -c "import shutil,glob;[shutil.rmtree(p) for p in glob.glob('**/__pycache__',recursive=True)]"
	cd $(API) && rm -rf .pytest_cache .mypy_cache .ruff_cache
