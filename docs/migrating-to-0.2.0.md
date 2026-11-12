# Migrating to 0.2.0

This guide is for maintainers moving a checkout from buildlimits 0.1.0 to
0.2.0. The release adds the FizzBee model check: a model of admission and
recovery, a pinned checker, and a `modelcheck` command that judges the model's
results. Nothing about an installed buildlimits changes. The wheel still
contains only `buildlimits`, no runtime behaviour, threshold, configuration
key, status document, metric or journal field moves, and an operator who only
runs the supervisor has nothing to do.

The design is recorded in
[ADR 008](adrs/adr-0008-model-check-outside-the-wheel.md); the operator-facing
behaviour is described under
[Check the admission model](users-guide.md#check-the-admission-model).

## What changes

| Area           | In 0.1.0                    | In 0.2.0                                               |
| -------------- | --------------------------- | ------------------------------------------------------ |
| Wheel contents | `buildlimits`               | `buildlimits` (unchanged)                              |
| Source tree    | `buildlimits/`, `tests/`    | Adds `modelcheck/` and `models/`                       |
| Make targets   | Build, test, lint, release  | Adds `fizzbee`, `model-fidelity` and `model-check`     |
| Command line   | `build-limits-*` scripts    | Adds `python -m modelcheck check` and `drift`          |
| Dependencies   | Runtime and development set | Unchanged; the checker is an external pinned binary    |
| CI             | Pull-request and release    | Adds a fidelity job per pull request and a nightly run |

_Table 1: What 0.2.0 adds._

The source distribution includes `modelcheck/` and `models/`. That is
deliberate: the model is verification apparatus that a maintainer reads, and
the package metadata does not change.

## Prerequisites

Model checking runs on x86-64 Linux. The Python baseline is 3.14, the same as
the package's, and the tooling uses only the standard library plus packages
already in the development group. Install `uv` if it is absent, then create the
environment once:

```bash
make build
```

The checker installer needs GNU `make`, `curl`, `tar` with gzip support,
`sha256sum` and `flock` from util-linux. A network connection is needed only
the first time, unless the pinned version or digest changes.

## Upgrade steps

1. Update the checkout and rebuild the environment:

   ```bash
   git pull
   make build
   ```

2. Fetch and verify the pinned checker:

   ```bash
   make fizzbee
   ```

   The target downloads the release named by `FIZZBEE_VERSION`, checks it
   against `FIZZBEE_SHA256`, unpacks it into a content-addressed directory
   under the user cache, and links `.tools/fizzbee` at the worktree root. A
   failed download or a digest mismatch installs nothing: the partial asset is
   removed, no cache entry is written, and the link is not created. A cache
   entry and link left by an earlier successful install are not removed, so a
   later run still finds the last verified checker rather than a partial one.
   The binary is never committed and never installed system-wide.

3. Run the cheap check, which compares the model's `# maps:` annotations with
   the production package:

   ```bash
   make model-fidelity
   ```

   It finishes in seconds and runs on every pull request.

4. Run the full matrix when a change touches admission or recovery:

   ```bash
   make model-check
   ```

   It takes minutes. Each configuration's expectations are judged, and
   `--fixpoint` re-runs every configuration that should pass at twice its
   declared `max_actions` bound, so a run truncated at its bound is reported
   rather than passed.

5. If a job needs the tool directly, invoke it through `uv run` so it uses the
   environment `make build` created:

   ```bash
   uv run python -m modelcheck check models/admission/coupling --fizz .tools/fizzbee/fizz
   uv run python -m modelcheck drift models/admission/coupling
   ```

   `--json PATH` writes the judgements as JSON, `--save-traces` keeps each
   counterexample's normalized trace under the model's `traces/` directory, and
   `--only CONFIG[:PROPERTY]` narrows the report without narrowing the check.

## Exit statuses

Both commands distinguish a wrong model from a tool that could not run.

| Status | Meaning                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------- |
| `0`    | Every expectation was met, or `drift` found a clean report.                                       |
| `1`    | An expectation was unmet, a fixpoint comparison grew, or `drift` found an unmapped or stale path. |
| `2`    | The tool could not run: a usage error or an input it cannot interpret.                            |

_Table 2: Exit statuses of `python -m modelcheck`._

`check` and `drift` share the statuses but ask different questions. `drift`
reports `0` when the model accounts for every production path it names, and `1`
when the report names a path the inventory does not hold (stale) or an
inventory item the model neither maps nor excludes (unmapped).

A `2` is never folded into `1`. A checker that never started is different: the
command did run, so the matrix records an error judgement naming the reason and
the status is `1`. With `--fixpoint`, a run that should pass and did not is
reported and then ends the command with `2`, so a script that treats `1` and
`2` differently should not assume a checker failure keeps the status at `1`.

## Generated artefacts

A run copies its specification into a fresh directory under the git-ignored
`.model-check/` and writes its output there, so the run itself writes no
tracked file. Deleting `.model-check/` is always safe. `make model-check` also
writes `.model-check/coupling.json` and `.model-check/transaction.json`, which
the end-to-end tests read.

`--save-traces` is the one declared exception: it keeps each counterexample's
normalized path under `models/admission/<half>/traces/`, which is deliberately
not git-ignored so a maintainer reviews the paths as diffs. After a run those
files show up as untracked until they are committed or removed. A failed
comparison's counterexample is kept there too, named
`<configuration>-fixpoint-<stage>.json`, so it does not overwrite the matrix's
trace for the same configuration.

The JSON record names the kind of result each failed run produced, and a run
that reported no exploration time of its own carries the time the harness
waited for it under a separate field rather than as the checker's duration.

## Rollback

The release adds no runtime behaviour, so rolling back is reverting the
checkout to the 0.1.0 tag and rebuilding. The checker cache under
`$XDG_CACHE_HOME/buildlimits/fizzbee/` and the `.tools/fizzbee` link may be
deleted; neither is read by the package. Trace files committed as evidence are
ordinary tracked files and are removed by the same revert.
