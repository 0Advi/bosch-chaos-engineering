PYTHON ?= python3

.PHONY: help bootstrap validate build start stop status smoke verify test schemas preflight flags capabilities \
	probe baseline derive-policy runs abort reconcile api report test-sandbox
help:
	@echo "bootstrap  Fetch pinned upstream source"
	@echo "validate   Validate the selected deployment without starting containers"
	@echo "build      Build application images from pinned source"
	@echo "start      Pull versioned images and start the 25-service sandbox"
	@echo "status     Show sandbox container status"
	@echo "smoke      Probe application APIs and Prometheus (not a full baseline)"
	@echo "verify     Check cart/checkout and telemetry; save startup evidence"
	@echo "stop       Stop this sandbox without deleting volumes"
	@echo "test       Run controller contract/safety tests (no Docker needed)"
	@echo "schemas    Regenerate JSON Schemas from controller contracts"
	@echo "preflight  Check sandbox identity and isolation (read-only)"
	@echo "flags      Verify the sandbox-owned flag file (and runtime if running)"
	@echo "capabilities  List the typed fault capability catalog"
	@echo "probe      One synthetic checkout correlated with traces/logs (read-only)"
	@echo "baseline   Measure a healthy payment baseline (about 4 minutes)"
	@echo "derive-policy BASELINES=\"a.json b.json\"  Write config/payment_policy.yaml"
	@echo "runs       Show unfinished fault runs"
	@echo "abort RUN=<id>  Abort a run; it is restored and recovery is verified"
	@echo "reconcile  Restore runs whose controller died (after a crash/restart)"
	@echo "api        Start the local controller API on http://127.0.0.1:8765"
	@echo "report RUN=<id>  Export evidence and print the run report"
	@echo "test-sandbox  Opt-in checks against the running dedicated sandbox"

bootstrap:
	git submodule update --init --recursive
validate:
	$(PYTHON) scripts/demo.py validate
build:
	$(PYTHON) scripts/demo.py build
start:
	$(PYTHON) scripts/demo.py start
stop:
	$(PYTHON) scripts/demo.py stop
status:
	$(PYTHON) scripts/demo.py status
smoke:
	$(PYTHON) scripts/demo.py smoke
verify:
	$(PYTHON) scripts/verify_startup.py
test:
	$(PYTHON) -m pytest -q tests
schemas:
	$(PYTHON) -m controller.schemas.export
preflight:
	$(PYTHON) -m controller.cli preflight
flags:
	$(PYTHON) scripts/verify_flags.py
capabilities:
	$(PYTHON) -m controller.cli capabilities
probe:
	$(PYTHON) -m controller.cli probe
baseline:
	$(PYTHON) -m controller.cli baseline
derive-policy:
	$(PYTHON) -m controller.cli derive-policy $(BASELINES) --write
runs:
	$(PYTHON) -m controller.cli status
abort:
	$(PYTHON) -m controller.cli abort $(RUN)
reconcile:
	$(PYTHON) -m controller.cli reconcile
api:
	$(PYTHON) -m controller.cli serve
report:
	$(PYTHON) -m controller.cli report $(RUN)
test-sandbox:
	BOSCH_SANDBOX_TESTS=1 $(PYTHON) -m pytest -q -s -m sandbox tests/integration
