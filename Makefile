# simple-talk
# Cross-platform build & dev targets. Requires GNU make + a Python 3.8+.
# On Windows use the "make" shipped with Git-for-Windows / MSYS2, or
# mingw32-make from a Python toolchain.

PY      ?= python3
PIP     ?= $(PY) -m pip
NUITKA  ?= $(PY) -m nuitka
OUT     ?= dist

# `make install` destinations (override on the command line, e.g.
# `make install PREFIX=/usr/local`, or use DESTDIR for packaging).
# Default PREFIX is ~/.local so no root is required.
PREFIX  ?=
BINDIR  ?=
DESTDIR ?=

CLIENT_MAIN := client/main.py
SERVER_MAIN := server/main.py
CLIENT_BIN  := $(OUT)/simple-talk
SERVER_BIN  := $(OUT)/simple-talk-server

# Nuitka options shared by both binaries.
#   --onefile                  -> a single self-contained executable
#   --assume-yes-for-downloads -> fetch ccache/zstd etc. during first build
#   --lto=yes                  -> link-time optimisation (smaller, faster)
#   --remove-output            -> delete intermediate build dir after success
NUITKA_FLAGS := --onefile --assume-yes-for-downloads --lto=yes --remove-output --output-dir=$(OUT)

.PHONY: default all client server deps install uninstall test dev run dev-server run-server clean help

## default: generic build -- compile both binaries (same as `make all`)
default: all

## all: build both the client and server binaries
all: client server

## deps: install Nuitka (with onefile extras) into the active Python env
deps:
	$(PIP) install "nuitka[onefile]"
	@$(PY) -c "import sys,subprocess; sys.platform.startswith('linux') and subprocess.check_call([sys.executable,'-m','pip','install','patchelf'])"

## client: build the client as a single binary with Nuitka
client:
	$(NUITKA) $(NUITKA_FLAGS) --output-filename=simple-talk $(CLIENT_MAIN)

## server: build the server binary with Nuitka (source-only is fine too)
server:
	$(NUITKA) $(NUITKA_FLAGS) --output-filename=simple-talk-server $(SERVER_MAIN)

## install: build if needed, then install binaries into $(PREFIX)/bin
install: all
	@ST_OUT="$(OUT)" ST_PREFIX="$(PREFIX)" ST_BINDIR="$(BINDIR)" ST_DESTDIR="$(DESTDIR)" $(PY) scripts/install.py

## uninstall: remove previously installed binaries
uninstall:
	@ST_PREFIX="$(PREFIX)" ST_BINDIR="$(BINDIR)" ST_DESTDIR="$(DESTDIR)" $(PY) scripts/install.py --uninstall

## dev: run the client from source (no compile step)
dev:
	$(PY) $(CLIENT_MAIN) $(ARGS)

## run: run the built client binary if present, else fall back to source
run:
	@if [ -x $(CLIENT_BIN) ] || [ -f $(CLIENT_BIN) ]; then $(CLIENT_BIN) $(ARGS); \
	else echo "no binary built yet -- running from source"; $(PY) $(CLIENT_MAIN) $(ARGS); fi

## dev-server: run the mesh server from source
dev-server:
	$(PY) $(SERVER_MAIN) $(ARGS)

## run-server: run the built server binary if present, else fall back to source
run-server:
	@if [ -x $(SERVER_BIN) ] || [ -f $(SERVER_BIN) ]; then $(SERVER_BIN) $(ARGS); \
	else echo "no binary built yet -- running from source"; $(PY) $(SERVER_MAIN) $(ARGS); fi

## test: end-to-end smoke test (exit 0 on success)
test:
	$(PY) scripts/smoke_test.py

## clean: remove build outputs and caches
clean:
	$(PY) -c "import shutil,glob,sys; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob(sys.argv[1])+glob.glob('.build')]" "$(OUT)"
	$(PY) -c "import shutil,glob; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob('**/__pycache__', recursive=True)]"

## help: show this message
help:
	@sed -n 's/^## /make /p' $(MAKEFILE_LIST) | column -t -s':'