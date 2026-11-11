PY ?= python3
LLM ?= mock
SCENARIO ?= 11
RUNS ?= 3

.PHONY: install demo demo-all list eval test lint

install:
	$(PY) -m pip install -e ".[dev]"

list:
	$(PY) -m diag list

demo:
	$(PY) -m diag demo --scenario $(SCENARIO) --llm $(LLM)

demo-all:
	@for s in 1 2 4 8 11 15 16 21 29 30; do $(PY) -m diag demo --scenario $$s --llm $(LLM) --quiet; done

eval:
	$(PY) -m diag eval --runs $(RUNS) --llm $(LLM)

test:
	$(PY) -m pytest -q

lint:
	ruff check diag tests
