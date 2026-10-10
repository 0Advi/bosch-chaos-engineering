PYTHON ?= python3

.PHONY: help bootstrap validate build start stop status smoke verify
help:
	@echo "bootstrap  Fetch pinned upstream source"
	@echo "validate   Validate the selected deployment without starting containers"
	@echo "build      Build application images from pinned source"
	@echo "start      Pull versioned images and start the 25-service sandbox"
	@echo "status     Show sandbox container status"
	@echo "smoke      Probe application APIs and Prometheus (not a full baseline)"
	@echo "verify     Check cart/checkout and telemetry; save startup evidence"
	@echo "stop       Stop this sandbox without deleting volumes"

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
