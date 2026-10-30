# Architectural decision record (ADR) 005: model-check tooling outside the wheel

## Status

Accepted on 2026-10-26. The FizzBee model check is a top-level `modelcheck/`
package, structured as a hexagon beside `buildlimits/` and excluded from the
built wheel.

## Date

2026-10-26.

## Context and problem statement

RFC 0001 asks whether the supervisor's admission and recovery behaviour keeps
its stated properties under a bounded model. Answering that needs a model, a
pinned checker and tooling that turns one checker run into a verdict a
maintainer can defend. The tooling has real policy: it decides what a
configuration expects, materializes each variant from one source, judges a
result against those expectations, computes drift between the model and the
code, and normalizes counterexample traces.

That policy must be testable without the 335 MB checker binary and without a
running supervisor. It also must not become part of the shipped product: the
package is verification apparatus, not runtime behaviour, and a wheel consumer
has no use for it. The build currently packages `buildlimits` only, which
settles the packaging half of the question but not where the code lives or how
it is structured.

## Decision Drivers

- Every piece of policy must be exercisable without the checker binary.
- The tooling must not import the product it verifies, except at one declared
  boundary.
- The build must not publish the tooling to wheel consumers.
- Existing gates — git source discovery, Skylos, Pylint, `ty`, Ruff — must
  cover the new code without new configuration beyond named exceptions.
- A checker run must leave no tracked file changed.

## Options considered

### Option A: a module under `buildlimits/`

The tooling would be importable from the product and share its package. It
would also ship in the wheel, so a consumer would install a checker driver they
cannot use, and the product would carry test-only policy. The layering check
the plan requires — that the model check does not depend on the code it checks
— would be impossible to state, let alone enforce. Rejected.

### Option B: a single-file `uv run --script` program under `scripts/`

`scripts/` is for single-file programs, and this tooling is several modules
with a domain, a service and four adapters. A single file would also defeat the
400-line limit and put policy and process control in one place. Rejected.

### Option C: a top-level package, structured as a hexagon, excluded from the wheel

`modelcheck/` sits beside `buildlimits/` at the repository root. The pure
modules hold the policy, `ports.py` names the three collaborators, the adapters
are the only modules that touch a process, a file or `buildlimits`, and
`cli.py` is a Cyclopts driving adapter. `[tool.hatch.build.targets.wheel]`
continues to package `buildlimits` only. Chosen.

## Decision outcome / proposed direction

The package is a hexagon:

- **Pure modules** — `domain`, `expectations`, `materialize`, `frontmatter`,
  `drift`, `trace`, `report`, `ports` and `service`. A structural test walks
  their syntax trees and fails on an import outside the allowlist or on any
  call to a banned name, so purity is checked rather than promised.
- **Adapters** — `fizzbee_process` (the only module that starts a process),
  `fizzbee_output` and `fizzbee_measurements` (the pure translation of what a
  run printed), `configuration` with its two field readers (the model
  directory's layout and its TOML schema), and `inventory` (the only module
  that imports `buildlimits`).
- **Driving adapter** — `cli.py` builds the Cyclopts application from adapter
  *factories*, so a test supplies doubles without a checker and without a model
  directory. `__main__.py` is the only module that constructs the real adapters.

Three consequences follow, and each is deliberate:

- `modelcheck` is not installed. It is importable because `pyproject.toml`
  puts the repository root on pytest's `pythonpath` and `testpaths` includes
  `modelcheck`, which is what lets the module doctests and the layering tests
  run.
- The checker is invoked through `subprocess` with a fixed argument list, no
  shell and a new session, and killed by process group. Ruff's `S404` and
  `S603` are ignored for that module alone, with the reason recorded beside the
  suppression.
- A run writes only under `.model-check/`, a git-ignored scratch root, and
  each configuration's specification is copied there before the checker sees
  it. The plan's acceptance test for this is that `git status --porcelain` is
  empty after a run.

## Goals and non-goals

### Goals

- Make every judgement in the model check a function of values, testable
  against recorded checker output rather than the checker.
- Keep the product and its wheel free of verification apparatus.
- Confine process spawning, file reading and production imports to named
  adapters.

### Non-goals

- Publish the tooling, or add it to the runtime dependency set. It adds no
  dependency at all; FizzBee is an external tool pinned by digest.
- Replace the existing unit, behavioural or host-acceptance suites. The model
  check is an additional obligation, not a substitute for any of them.
- Decide whether the model check joins `make all`. That is gate G4.

## Known risks and limitations

- A package outside the wheel is not exercised by the wheel's own import
  checks, so a broken import is caught by the tests and the layering guards
  rather than by packaging.
- The structural purity test is an allowlist over syntax trees. It catches an
  import or a banned call, not an effect reached through a value a caller
  passes in; the adapters' protocols are the design answer to that, and the
  test is a guard rather than a proof.
- The checker's output format is an external contract. The parser is
  fail-closed and its recordings are replayed by
  `tests/test_fizzbee_fixtures.py`, so a checker version that changes a line
  fails there rather than silently changing a verdict.

## Architectural rationale

The hexagon is what makes the central claim checkable. A verdict is
`expectation × result → judgement`, and both sides are values, so the
behavioural scenarios can state what a maintainer must be able to rely on
without running the 335 MB binary. Keeping the package outside the wheel states
the same boundary in packaging terms: the tooling exists to verify the product
and must never become part of it.

## References

- [RFC 0001: FizzBee model of admission and recovery](rfcs/0001-fizzbee-model-of-admission-and-recovery.md)
- [Developers' guide §11.6](developers-guide.md#116-model-checking)
- [ExecPlan: fizzbee-model-of-admission-and-recovery](execplans/fizzbee-model-of-admission-and-recovery.md)
