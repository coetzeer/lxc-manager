SHELL := /bin/bash
.DEFAULT_GOAL := help
PROJECT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
VENV := $(PROJECT_DIR).venv
PYTHON ?= python3
HOST ?= 127.0.0.1
PORT ?= 8080

.PHONY: help venv install install-pam test test-ui start dev
help:
	@echo "make install  Create .venv and install runtime dependencies"
	@echo "              Also install the distro-specific PAM service (may use sudo)"
	@echo "make test     Install test dependencies and run isolated tests"
	@echo "make test-ui  Run dashboard state regression tests (requires Node.js)"
	@echo "make start    Install dependencies and serve dashboard (PORT=8080)"
	@echo "make dev      Start with automatic reload"

$(VENV)/bin/python:
	$(PYTHON) -m venv "$(VENV)"

venv: $(VENV)/bin/python

$(VENV)/.runtime-ready: $(PROJECT_DIR)requirements.txt | venv
	"$(VENV)/bin/python" -m pip install -r "$(PROJECT_DIR)requirements.txt"
	touch "$@"

install: $(VENV)/.runtime-ready install-pam

install-pam:
	@set -eu; \
	if [ ! -r /etc/os-release ]; then \
		echo "Cannot detect distribution: /etc/os-release is missing." >&2; exit 1; \
	fi; \
	. /etc/os-release; \
	pam_source=''; \
	case "$${ID:-}:$${ID_LIKE:-}" in \
		debian:*|ubuntu:*|linuxmint:*|*:debian*|*:ubuntu*) pam_source="$(PROJECT_DIR)docs/pam/lxc-manager.debian" ;; \
		fedora:*|rhel:*|centos:*|rocky:*|almalinux:*|ol:*|*:fedora*|*:rhel*|*:redhat*) pam_source="$(PROJECT_DIR)docs/pam/lxc-manager.redhat" ;; \
		arch:*|manjaro:*|endeavouros:*|*:arch*) pam_source="$(PROJECT_DIR)docs/pam/lxc-manager.arch" ;; \
		*) echo "Unsupported Linux distribution: ID=$${ID:-unknown} ID_LIKE=$${ID_LIKE:-unknown}" >&2; exit 1 ;; \
	esac; \
	if [ -f /etc/pam.d/lxc-manager ] && cmp -s "$$pam_source" /etc/pam.d/lxc-manager; then \
		echo "PAM service already current: /etc/pam.d/lxc-manager"; exit 0; \
	fi; \
	if [ "$$(id -u)" -eq 0 ]; then \
		install -m 0644 "$$pam_source" /etc/pam.d/lxc-manager; \
	elif command -v sudo >/dev/null 2>&1; then \
		sudo install -m 0644 "$$pam_source" /etc/pam.d/lxc-manager; \
	else \
		echo "Installing /etc/pam.d/lxc-manager requires root or sudo." >&2; exit 1; \
	fi; \
	echo "Installed $$pam_source as /etc/pam.d/lxc-manager"

$(VENV)/.tests-ready: $(PROJECT_DIR)requirements-dev.txt $(VENV)/.runtime-ready
	"$(VENV)/bin/python" -m pip install -r "$(PROJECT_DIR)requirements-dev.txt"
	touch "$@"

test: $(VENV)/.tests-ready
	cd "$(PROJECT_DIR)" && "$(VENV)/bin/python" -m unittest discover -s tests -v

test-ui:
	cd "$(PROJECT_DIR)" && node --test tests/test_dashboard.cjs

start: install
	cd "$(PROJECT_DIR)" && "$(VENV)/bin/python" -m uvicorn main:app --host "$(HOST)" --port "$(PORT)"

dev: install
	cd "$(PROJECT_DIR)" && "$(VENV)/bin/python" -m uvicorn main:app --host "$(HOST)" --port "$(PORT)" --reload
