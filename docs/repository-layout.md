# Repository layout

This document is the canonical map of the repository: where source, tests,
generated files and long-lived references live, and the conventions each path
follows. The tree below is simplified to the paths a contributor needs; every
omitted file sits under one of the directories it shows.

```plaintext
.
├── buildlimits/            the installable package; one module per decision
├── models/                 FizzBee specifications, grouped by question or workstream
│   └── spike/              toy models answering RFC 0001's language questions
├── tests/                  every pytest test and its shared support modules
│   ├── features/           pytest-bdd scenarios for admission and recovery
│   ├── fixtures/           journal samples, the fake sccache, the NOFILE smoke
│   │   └── fizzbee/        captured checker output, one directory per output class
│   └── __snapshots__/      Syrupy snapshots of rendered output
├── docs/                   guides, style rules and engineering references
├── scripts/                repository automation helpers
├── .github/                CI workflows and composite actions
├── .rules/                 Python style rules referenced from AGENTS.md
├── pyproject.toml          package metadata, entry points and tool policy
├── Makefile                the quality gates
├── typos.toml              generated spelling configuration; never edit by hand
└── typos.local.toml        repository-specific spelling exceptions
```

_Figure 1: Simplified repository tree._

| Path                      | Responsibility and conventions                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `buildlimits/`            | The admission core uses the standard library; tracing uses the OpenTelemetry SDK (see ADR 003). Modules stay under 400 lines. Console scripts are declared in `pyproject.toml` under `[project.scripts]`. Module docstrings may carry doctests, which `make test` runs.                                                                                                        |
| `models/`                 | FizzBee specifications. Each file states the question or property it answers in a leading comment, because the checker's verdict means nothing without it. Until `make model-check` arrives in EP-M4, the spike tests run an explicitly opted-in `BUILD_LIMITS_MODEL_CHECK=1 uv run pytest -n 0 -m fizzbee`, and each run compiles a scratch copy rather than the file itself. |
| `models/spike/`           | Toy models answering RFC 0001's open questions 1, 2 and 6, with the paired negative controls that separate a fairness or truncation effect from the property under test.                                                                                                                                                                                                       |
| `tests/`                  | All tests. `test_*.py` modules hold tests; other modules are shared helpers, linted as production code. See the developer's guide §11 for the helper inventory.                                                                                                                                                                                                                |
| `tests/features/`         | Gherkin scenarios. Each feature file is bound by a `scenarios()` call in the `test_*.py` module that holds its step definitions, such as `test_admission_rules_behaviour.py` or `test_pressure_systemd.py`.                                                                                                                                                                    |
| `tests/fixtures/`         | Data and stand-alone fixture programs. `fake_sccache.py` mirrors the real server's process model; `nofile_smoke.py` runs as a stand-alone script.                                                                                                                                                                                                                              |
| `tests/fixtures/fizzbee/` | Captured checker runs, one directory per output class (pass, safety failure, lasso, stutter, deadlock, truncation, parse error): the standard output, the exit code and, where the run failed, its error graph. `tests/test_fizzbee_fixtures.py` re-runs every class and pins its standard output and exit code, and the graphs each failing class writes.                     |
| `tests/__snapshots__/`    | Generated by Syrupy. Update with `pytest --snapshot-update` only when the rendered contract changes on purpose, and review the diff.                                                                                                                                                                                                                                           |
| `docs/`                   | Documentation, indexed by [contents](contents.md). The user's and developer's guides track behaviour and internal interfaces respectively.                                                                                                                                                                                                                                     |
| `scripts/`                | Automation helpers, following the [scripting standards](scripting-standards.md).                                                                                                                                                                                                                                                                                               |
| `.github/`                | Workflows and composite actions, validated by `make lint-actions`.                                                                                                                                                                                                                                                                                                             |
| `pyproject.toml`          | Packaging, dependency groups and the policy of every tool: Ruff, Pylint, Skylos, Vulture, the duplication gate and pytest. Static-analysis exceptions carry their reasons here.                                                                                                                                                                                                |

_Table 1: Path responsibilities._

Generated and local-only paths stay out of version control: `.venv`,
`.uv-cache`, `.uv-tools`, `.tools` (the pinned duplication detector and a link
to the FizzBee cache) and build output under `dist/`. A checker run also leaves
`.model-check/`, `models/*/*.json` and `models/**/out/`, which are ignored so a
manual run beside a source model is never staged by accident.
