# The CV-005 CodeScene contracts live in shared-actions and run from a full
# commit, so a fix is a pin bump. `.github/cv005.toml` holds this repository's
# only parameters.
CV005_CONTRACTS_REF ?= 5a99aed78ce68dcebd3052b75c31e6f173162b1e
CV005_CONTRACTS = $(UV_ENV) $(UV) tool run --python 3.13 --from 'git+https://github.com/leynos/shared-actions@$(CV005_CONTRACTS_REF)\#subdirectory=packages/cv005-contracts' cv005-contracts
MDLINT ?= markdownlint-cli2
NIXIE ?= nixie
MDTABLEFIX ?= mdtablefix
MDTABLEFIX_VERSION ?= 0.6.1
MDTABLEFIX_SELECT = --git --include-untracked
MDTABLEFIX_RULES = --wrap --renumber --breaks --ellipsis --fences
ACTIONLINT ?= actionlint
ACTION_VALIDATOR ?= action-validator
export PATH := $(HOME)/.local/bin:$(HOME)/.bun/bin:$(PATH)
UV ?= $(shell command -v uv 2>/dev/null || printf '%s/.local/bin/uv' "$$HOME")
USER_CARGO := $(HOME)/.cargo/bin/cargo
USER_WHITAKER := $(HOME)/.local/bin/whitaker
USER_BIN_PATH := $(HOME)/.cargo/bin:$(HOME)/.local/bin:$(HOME)/.bun/bin
TOOLS = $(MDTABLEFIX) $(MDLINT) $(ACTIONLINT) $(ACTION_VALIDATOR)
VENV_TOOLS = pytest
UV_ENV = PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools
TYPOS_CONFIG_BUILDER_VERSION ?= v0.1.3
TYPOS_CONFIG_BUILDER = env $(UV_ENV) $(UV) tool run --from \
	"git+https://github.com/leynos/typos-config-builder.git@$(TYPOS_CONFIG_BUILDER_VERSION)" \
	typos-config-builder
WITH_ACT ?= 0
ACT_TEST_ENV = $(if $(filter 1 true yes on,$(WITH_ACT)),RUN_ACT_VALIDATION=1,)
PYTEST_XDIST_WORKERS ?= auto
# Discover tracked and new Python sources, including hidden action directories.
PYTHON_TARGETS_FROM_GIT := $(if $(filter undefined,$(origin PYTHON_TARGETS)),1,0)
PYTHON_TARGETS ?=
RUFF_VERSION ?= 0.16.8
TY_VERSION ?= 0.0.89
LINT_PYTHON ?= 3.14
RUFF = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from 'ruff==$(RUFF_VERSION)' ruff
TY = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from 'ty==$(TY_VERSION)' ty
DF12_PYTHON_LINTS_REF ?= v0.4.0
DF12_PYTHON_LINTS = git+https://github.com/leynos/df12-python-lints.git@$(DF12_PYTHON_LINTS_REF)
DF12_PYLINT_MESSAGES = R9101,C9102,R9103,R9104,C9105,C9106,C9107,R9108,R9109,R9110,R9111,R9112,C9112
DF12_PYLINT = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from 'pylint==$(PYLINT_VERSION)' --with '$(DF12_PYTHON_LINTS)' pylint --disable=all --load-plugins=df12_python_lints --enable=syntax-error,$(DF12_PYLINT_MESSAGES)
AMBRLEAKS = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from '$(DF12_PYTHON_LINTS)' ambrleaks
SKYLOS = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from 'df12-python-lints[skylos] @ $(DF12_PYTHON_LINTS)' df12-skylos
DUPLICATION_GATE = $(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from '$(DF12_PYTHON_LINTS)' df12-duplication
# Pylint runs on CPython at the project baseline: no managed PyPy parses
# Python 3.14 syntax.
PYLINT_PYTHON ?= cpython@3.14
PYLINT_VERSION ?= 4.0.9
PYLINT_TARGETS ?= $(PYTHON_TARGETS)
PYLINT = $(UV_ENV) $(UV) tool run --managed-python --python $(PYLINT_PYTHON) --from 'pylint==$(PYLINT_VERSION)' pylint
# FizzBee model checker (RFC 0001). The release is pinned by digest and cached
# under the user's XDG cache, so every checkout shares one verified download.
# FIZZBEE_HOME and FIZZBEE_STAMP are spelled with make's $(or ...) rather than
# the shell's ${XDG_CACHE_HOME:-$HOME/.cache} because make must resolve the
# stamp path to decide whether a refetch is needed. The asset name and the
# digest move together: overriding one without the other fails the check.
FIZZBEE_VERSION ?= v0.5.4
FIZZBEE_SHA256 ?= 8a1d0900f9ad920002151922337912e11f9e588a2f1196249fe40bf75370f782
FIZZBEE_ASSET ?= fizzbee-$(FIZZBEE_VERSION)-linux_x86.tar.gz
FIZZBEE_URL ?= https://github.com/fizzbee-io/fizzbee/releases/download/$(FIZZBEE_VERSION)/$(FIZZBEE_ASSET)
# Fallback pin only. The image wraps the same release and carries SLSA
# provenance, but no target pulls or runs it: the tarball needs no container
# runtime. It is recorded so a later decision to containerize starts from a
# verified digest. Its tag is 0.5.4; a v-prefixed tag does not exist.
FIZZBEE_IMAGE ?= ghcr.io/fizzbee-io/fizzbee@sha256:e7b90a0157ab466283e96a9650ab9f91b8ab80a9db8ac6f1d868ecd2b2c687b8
FIZZBEE_HOME ?= $(or $(XDG_CACHE_HOME),$(HOME)/.cache)/buildlimits/fizzbee/$(FIZZBEE_SHA256)
FIZZBEE_STAMP ?= $(FIZZBEE_HOME)/.verified
FIZZ ?= .tools/fizzbee/fizz


.PHONY: help all audit clean build build-release fizzbee lint lint-python fmt \
        check-fmt check-lock check-python-sources \
        markdownlint nixie spelling test test-systemd-user test-sccache-host typecheck dead-code duplication install-nose lint-actions validate $(TOOLS) $(VENV_TOOLS)

.DEFAULT_GOAL := all
# Shared build caches require sequential gates even when make receives -j.
.NOTPARALLEL:

all: check-lock check-python-sources build check-fmt lint typecheck test test-workflow-contracts
	+$(MAKE) spelling

define ensure_uv
	@command -v $(UV) >/dev/null 2>&1 || { \
	  printf "Error: uv is required, but '%s' was not found or is not executable\n" "$(UV)" >&2; \
	  exit 1; \
	}
endef

check-lock: ## Check that uv.lock matches project metadata
	$(UV_ENV) $(UV) lock --check

.venv: pyproject.toml
	$(call ensure_uv)
	$(UV_ENV) $(UV) venv --clear

build: check-lock .venv ## Build virtual-env and install deps
	$(UV_ENV) $(UV) sync --group dev

build-release: check-lock ## Build artefacts (sdist & wheel)
	$(call ensure_uv)
	$(UV_ENV) $(UV) run python -m build --sdist --wheel

clean: ## Remove build artefacts
	rm -rf build dist *.egg-info \
	  .mypy_cache .pytest_cache .coverage coverage.* \
	  lcov.info htmlcov .venv .uv-cache .uv-tools \
	  .typos-oxendict-base.json .typos-oxendict-base.toml
	find . -type d -name '__pycache__' -print0 | xargs -0 -r rm -rf

define ensure_tool
	@command -v $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required, but not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

define ensure_tool_venv
	@$(UV_ENV) $(UV) run which $(1) >/dev/null 2>&1 || { \
	  printf "Error: '%s' is required in the virtualenv, but is not installed\n" "$(1)" >&2; \
	  exit 1; \
	}
endef

ifneq ($(strip $(TOOLS)),)
$(TOOLS): ## Verify required CLI tools
	$(call ensure_tool,$@)
endif


ifneq ($(strip $(VENV_TOOLS)),)
.PHONY: $(VENV_TOOLS)
$(VENV_TOOLS): build ## Verify required CLI tools in venv
	$(call ensure_tool_venv,$@)
endif


ifeq ($(PYTHON_TARGETS_FROM_GIT),1)
# Recipe expansion runs once, after prerequisites; check this capture's status
# before consumers expand their shared, simply expanded source list.
check-python-sources:
	$(eval PYTHON_TARGETS := $(shell git ls-files --cached --others --exclude-standard '*.py' '*.pyi'))
	$(if $(filter 0,$(.SHELLSTATUS)),,$(error Error: git ls-files failed while discovering Python sources for PYTHON_TARGETS))
	@:
else
check-python-sources:
	@:
endif

fmt: check-python-sources build $(MDTABLEFIX) ## Format sources
	$(RUFF) format $(PYTHON_TARGETS)
	$(RUFF) check --select I --fix $(PYTHON_TARGETS)

	$(MDTABLEFIX) --in-place $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

check-fmt: check-python-sources build $(MDTABLEFIX) ## Verify formatting
	$(RUFF) format --check $(PYTHON_TARGETS)

	@test "$$($(MDTABLEFIX) --version)" = "mdtablefix $(MDTABLEFIX_VERSION)" || { \
	  printf 'Error: mdtablefix %s is required\n' '$(MDTABLEFIX_VERSION)' >&2; exit 1; \
	}
	$(MDTABLEFIX) --check $(MDTABLEFIX_SELECT) $(MDTABLEFIX_RULES)

lint: lint-python dead-code duplication lint-actions ## Run linters

lint-python: check-python-sources build ## Run Python linters
	$(RUFF) check $(PYTHON_TARGETS)
	$(UV_ENV) $(UV) run interrogate --fail-under 100 $(PYTHON_TARGETS)
	$(PYLINT) $(PYLINT_TARGETS)
	$(DF12_PYLINT) $(PYTHON_TARGETS)
	$(AMBRLEAKS) tests


typecheck: check-python-sources build ## Run typechecking
	$(TY) --version
	$(TY) check --python .venv $(PYTHON_TARGETS)

audit: build ## Audit dependencies for known vulnerabilities
	$(UV_ENV) $(UV) run pip-audit


markdownlint: $(MDLINT) ## Lint Markdown files and spelling
	env -u NO_COLOR $(MDLINT) '**/*.md'
	+$(MAKE) spelling

spelling: ## Enforce en-GB-oxendict spelling
	$(TYPOS_CONFIG_BUILDER) gate --repository . --scope all

nixie: ## Validate Mermaid diagrams
	$(call ensure_tool,$(NIXIE))
	$(NIXIE) --no-sandbox

test: build $(VENV_TOOLS) ## Run tests
	$(UV_ENV) $(ACT_TEST_ENV) $(UV) run pytest -v -n $(PYTEST_XDIST_WORKERS)

# Host acceptance drives the developer's own systemd user manager with
# uniquely named units, so it runs only when asked for and never under xdist.
test-systemd-user: build $(VENV_TOOLS) ## Run host acceptance against the systemd user manager
	BUILD_LIMITS_HOST_ACCEPTANCE=1 $(UV_ENV) $(UV) run pytest -v -m systemd_user tests

test-sccache-host: build $(VENV_TOOLS) ## Run opt-in checks against a supported sccache
	BUILD_LIMITS_SCCACHE_HOST_CHECK=1 $(UV_ENV) $(UV) run pytest -v -m sccache_host_check tests


validate: ## Validate the Makefile
	mbake validate Makefile

dead-code: ## Run the shared strict Skylos gate
	$(SKYLOS) check --repository .
	$(UV_ENV) $(UV) tool run --python $(LINT_PYTHON) --from 'vulture==2.16' vulture tests

install-nose: ## Install the pinned detector with the shared verified installer
	$(DUPLICATION_GATE) install --repository .

duplication: install-nose ## Check every duplication family with the shared gate
	$(DUPLICATION_GATE) check --repository .

# The stamp carries the digest, so a cache entry left by an interrupted unpack,
# or by another version, is never mistaken for a verified one. The recipe holds
# a lock so two checkouts fetching at once still unpack once.
$(FIZZBEE_STAMP):
	@set -eu; \
	  want='$(FIZZBEE_SHA256)'; \
	  home='$(FIZZBEE_HOME)'; \
	  asset="$$home.tar.gz"; \
	  case "$$home" in \
	    */buildlimits/fizzbee/"$$want") ;; \
	    *) printf 'Error: FIZZBEE_HOME must end in /buildlimits/fizzbee/%s\n' "$$want" >&2; exit 1 ;; \
	  esac; \
	  mkdir -p "$$(dirname "$$home")"; \
	  ( \
	    flock 9; \
	    if [ "$$(cat "$$home/.verified" 2>/dev/null || :)" = "$$want" ]; then exit 0; fi; \
	    printf 'Fetching %s\n' '$(FIZZBEE_URL)'; \
	    rm -f "$$asset.part"; \
	    curl --fail --location --retry 3 --retry-all-errors \
	      --connect-timeout 20 --max-time 900 \
	      --output "$$asset.part" '$(FIZZBEE_URL)'; \
	    mv -f "$$asset.part" "$$asset"; \
	    if ! printf '%s  %s\n' "$$want" "$$asset" | sha256sum --check --status -; then \
	      printf 'Error: upstream asset changed; do not update FIZZBEE_SHA256 without review\n' >&2; \
	      printf 'Expected %s\n' "$$want" >&2; \
	      sha256sum "$$asset" >&2 || :; \
	      rm -f "$$asset"; \
	      exit 1; \
	    fi; \
	    if tar --list --gzip --file "$$asset" | grep -Eq '^/|(^|/)\.\.(/|$$)'; then \
	      printf 'Error: %s contains unsafe member paths\n' '$(FIZZBEE_ASSET)' >&2; \
	      rm -f "$$asset"; \
	      exit 1; \
	    fi; \
	    unpack=''; trap 'rm -rf "$$unpack"' EXIT HUP INT TERM; \
	    unpack="$$(mktemp -d "$$(dirname "$$home")/.unpack.XXXXXX")"; \
	    tar --extract --gzip --file "$$asset" --directory "$$unpack" \
	      --no-same-owner --no-same-permissions; \
	    rm -rf "$$home"; \
	    mv -T "$$unpack/$(FIZZBEE_ASSET:.tar.gz=)" "$$home"; \
	    rm -rf "$$unpack"; \
	    printf '%s\n' "$$want" > "$$home/.verified"; \
	    rm -f "$$asset"; \
	  ) 9>"$$home.lock"

fizzbee: $(FIZZBEE_STAMP) ## Install the pinned FizzBee checker
	@set -eu; mkdir -p .tools; ln -sfn '$(abspath $(FIZZBEE_HOME))' .tools/fizzbee

lint-actions: build $(ACTIONLINT) $(ACTION_VALIDATOR) ## Validate workflows and local actions
	$(call ensure_tool,shellcheck)
	$(UV_ENV) $(UV) tool run --from 'yamllint==1.38.0' yamllint --strict .github
	$(ACTIONLINT)
	@set -eu; tmpdir=$$(mktemp -d /tmp/template-actions-XXXXXX); \
	workflow="$$tmpdir/workflow.yml"; actions="$$tmpdir/actions"; \
	trap 'rm -rf "$$tmpdir"' EXIT; \
	find .github/actions -type f \( -name action.yml -o -name action.yaml \) -print > "$$actions"; \
	while IFS= read -r action; do \
	  $(ACTION_VALIDATOR) "$$action"; \
	  $(UV_ENV) $(UV) run --script scripts/action_workflow.py "$$action" > "$$workflow"; \
	  if [ -s "$$workflow" ]; then \
	    $(ACTIONLINT) -stdin-filename "$$action" - < "$$workflow"; \
	  fi; \
	done < "$$actions"

.PHONY: test-workflow-contracts
test-workflow-contracts: ## Check the CV-005 CodeScene workflow contracts
	$(CV005_CONTRACTS) check --repository .

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | \
	awk 'BEGIN {FS=":.*##"; printf "Available targets:\n"} {gsub(/^[[:space:]]+/, "", $$2); printf "  %-20s %s\n", $$1, $$2}'
