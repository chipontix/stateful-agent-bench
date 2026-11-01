# Verifiers run on the host against the shared brokers in infra/; agents run
# in a container with --network none. See README "Commands".
SHELL := /bin/bash
PY := python3
KAFKA ?= docker   # docker | native (host JVM, for a Docker VM too loaded to run a broker)
REPEAT ?= 3

.PHONY: test deps up down verify-all mutation runs runs-llm audit clean

test:              ## tooling self-checks: no brokers, no Docker
	$(PY) -m unittest discover -s tests -v

deps:              ## upstream clones + the Python env of the lakehouse verifier
	$(PY) -c 'from bench import workspace as w; [w.upstream(t) for t in w.all_tasks()]'
	test -x .cache/venv-lakehouse/bin/python || uv venv -q -p 3.12 .cache/venv-lakehouse
	uv pip install -q -p .cache/venv-lakehouse -r verifiers/lakehouse/requirements.txt

up:                ## Postgres (Docker) and Kafka (Docker or native)
	docker compose -f infra/compose.yaml up -d --wait postgres
	if [ "$(strip $(KAFKA))" = native ]; then infra/kafka-native.sh start; \
	else docker compose -f infra/compose.yaml up -d --wait kafka; fi

down:
	-infra/kafka-native.sh stop
	docker compose -f infra/compose.yaml down -v

verify-all: deps   ## per task: bug red, reference green REPEAT times -> results/verify-all.json
	$(PY) -m bench verify-all --repeat $(REPEAT)

mutation: deps     ## mutants of every reference fix -> results/mutation.{json,md}
	$(PY) -m bench mutation

runs: deps         ## stub agents in the no-network container -> results/run-*.json, results/traces/
	$(PY) -m bench run --agent stub-ref -n 1
	$(PY) -m bench run --agent stub-wrong -n 1
	$(PY) -m bench run --agent stub-cheat -n 1
	$(PY) -m bench run --agent stub-coin -n 4 -k 2

runs-llm: deps     ## the local-model agent (llama-server + model in the HF cache), 5 attempts per task
	$(PY) -m bench run --agent llm-qwen-coder-1.5b -n 5 -k 5 --phase agent
	$(PY) -m bench run --agent llm-qwen-coder-1.5b -n 5 -k 5 --phase verify
	$(PY) scripts/llm_analysis.py

clean:             ## work dirs and caches (not results/)
	rm -rf work .cache/go-build
