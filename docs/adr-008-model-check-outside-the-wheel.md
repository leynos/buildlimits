# Architectural decision record (ADR) 008: model-check tooling outside the wheel

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
  `drift`, `trace`, `report`, `records`, `ports`, `service`, `assertions`
  (which reads, requires and strips the assertions a configuration names),
  `fixpoint` with `fixpoint_bounds` (the bound the comparison reads and
  doubles) and `fixpoint_failures` (the evidence a failed comparison carries),
  and `selection` (the `--only` policy). A structural test walks their syntax
  trees and fails on an import outside the allowlist or on any call to a banned
  name, so purity is checked rather than promised.
- **Adapters** — `fizzbee_process` (the only module that starts a process) with
  `fizzbee_process_group` (the group kill and the bounded wait for it),
  `fizzbee_output`, `fizzbee_graph`, `fizzbee_measurements`, `fizzbee_evidence`
  and `fizzbee_redaction` (the pure translation of what a run printed: verdict
  lines are classified in `fizzbee_output`, error-graph records are decoded
  into domain links and traces in `fizzbee_graph`, the bounded transcript a
  failed run keeps is assembled in `fizzbee_evidence`, and the runner paths and
  credentials that must not be kept are replaced in `fizzbee_redaction`, so no
  checker key reaches a pure module), `configuration` with its two field
  readers (the model directory's layout and its TOML schema), and `inventory`
  (the only module that imports `buildlimits`).
- **Driving adapter** — `cli.py` builds the Cyclopts application from adapter
  *factories*, so a test supplies doubles without a checker and without a model
  directory. `cli_outputs.py` holds the artefact and the traces that run
  writes, beside the parsing that decides when to write them. `__main__.py` is
  the only module that constructs the real adapters.

Three consequences follow, and each is deliberate:

- `modelcheck` is not installed. It is importable because `pyproject.toml`
  puts the repository root on pytest's `pythonpath` and `testpaths` includes
  `modelcheck`, which is what lets the module doctests and the layering tests
  run.
- The checker is invoked through `subprocess` with a fixed argument list, no
  shell and a new session, and killed by process group. Ruff's `S404` and
  `S603` are ignored for that module alone, with the reason recorded beside the
  suppression.
- A run writes checker scratch output only under `.model-check/`, a
  git-ignored scratch root, and each configuration's specification is copied
  there before the checker sees it. `--save-traces` is the one declared
  exception: it writes each counterexample's normalized path under
  `models/admission/<half>/traces/`, which is deliberately not ignored so the
  evidence is reviewed as a diff. The plan's acceptance test is that
  `git status --porcelain` is empty after a run that was not asked for traces,
  and that a run that was asked adds only those files.

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

## Addendum: decisions taken after acceptance

The decision above is unchanged. The entries below record how the boundary was
applied during implementation, so a reader of this ADR sees the same
conclusions the ExecPlan's decision log carries. Each is recorded in full, with
its finding, reason and evidence, in the
[ExecPlan's decision log](execplans/fizzbee-model-of-admission-and-recovery.md#decision-log).

- **D37, 2026-11-03.** A liveness violation whose error graph is missing is
  classified `UNKNOWN`, not `LASSO`: a shape no run demonstrated must not meet
  an expectation that names it. Malformed evidence becomes `Errored` rather
  than a guessed path.
- **D38, 2026-11-04.** The checker's run-record vocabulary stops at the
  adapter. `adapters/fizzbee_graph` decodes error-graph keys into `trace.Link`
  values, and `tests/test_modelcheck_vocabulary.py` keeps those keys out of
  every pure module. The model's *source* syntax stays in the pure modules: it
  is the domain's input, which is what this ADR and EP-M2 place there
  deliberately.
- **D39, 2026-11-04.** `fails_as` refuses `unknown`, and the users' guide
  matches the flags it documents, including where `--save-traces` writes:
  `models/admission/<half>/traces/`, whose seven `head-*.json` traces are
  tracked evidence rather than untracked output. Corrected 2026-11-07; the
  original entry called the directory untracked.
- **D40, 2026-11-05.** A failed comparison keeps its evidence: `FixpointError`
  carries every failed run, and the command writes them into the JSON artefact
  before the error leaves. The cleanliness requirement distinguishes checker
  scratch output under `.model-check/` from requested trace evidence.
- **D41, 2026-11-06.** A completed comparison is recorded with the bounds and
  durations it was measured at, not only its counts. `Fixpoint` retains both
  `Passed` runs, so the artefact can state `bound`, `doubled_bound`,
  `elapsed_seconds` and `doubled_elapsed_seconds` beside the two state counts.
  A count without its bound is not evidence that the second run was given a
  larger bound at all.
- **D42, 2026-11-06.** A failed comparison's record says what kind of result it
  produced, and its evidence is kept the way a matrix counterexample's is. The
  record carries `result` (`passed`, `counterexample`, `stopped` or `errored`)
  beside the verdict, because "failed" alone cannot tell a run that found a
  violation from one that never answered. A run that reported no exploration
  time is timed by the harness and recorded as `harness_seconds`, never as
  `elapsed_seconds`: the two measure different things, and writing the second
  under the first name would claim a duration the checker never gave. A failed
  comparison's counterexample path is saved as
  `<configuration>-fixpoint-<stage>.json`, so it sits beside the matrix's own
  trace for the same configuration rather than overwriting it. The error graph
  is redacted of the run's scratch directory before it becomes evidence, on the
  same reasoning as the transcript: a runner path is not a fact about the
  model, and the graph is committed.
- **D43, 2026-11-06.** Evidence is redacted of everything that describes the
  runner, not only the scratch directory. The checker inherits the caller's
  environment and can print any of it, so the launcher path, the caller's home
  and temporary directories, and the value of every credential-named variable
  are replaced beside the scratch directory. The adapter cannot tell a secret
  from any other value, so the variable's name is the only signal available and
  short values are left alone: a value under six characters is a flag or a
  mode, and replacing it would mangle ordinary words without protecting
  anything. The line a verdict is read from — an `Error:` diagnostic or the
  marker a signal left — is bounded on the same reasoning as the transcript,
  because the checker writes it and its length is the checker's choice. The
  length rule stated here is superseded by D44, and its one-character residual
  by D50.
- **D44, 2026-11-06.** A credential is redacted in every form the checker can
  write it, and a short one is redacted too. This supersedes the length rule in
  D43. A checker that writes its graph as JSON escapes a quote or a backslash
  in the value it writes, so the document holds `a\"b` where the credential is
  `a"b`; the escaped form is now a needle beside the literal value, or the same
  credential would reach the record in the one shape the checker used. The
  six-character floor becomes a two-character floor plus a whole-token rule: a
  value shorter than six characters is replaced only where it stands alone. A
  five-character credential printed as `NAME=abcde` is therefore removed, while
  the same characters inside a longer word are kept. Two residuals remain and
  are recorded rather than hidden. A two- to five-character value beside a
  letter, digit or underscore is kept. A one-character value is not replaced at
  all. It is indistinguishable from a state count, a flag or an exit status —
  `Unique states: 1` is the very evidence a record exists to carry — so
  replacing every `1` in a transcript would destroy the report without
  protecting a secret it can no longer express. Its one-character residual is
  superseded by D50 and D53.
- **D47, 2026-11-07.** A trace is redacted again after the graph is decoded.
  Text redaction cannot see every spelling the checker writes: the checker's Go
  JSON encoder writes `<`, `>` and `&` as six-character Unicode escapes, and a
  value mixing an escaped quote with a literal non-ASCII character matches
  neither spelling Python produces, so a needle derived from the value's Python
  form misses the document's bytes. Once the graph is decoded, every spelling
  has collapsed to one string, so `adapters/fizzbee_trace_redaction` walks each
  state of the parsed counterexample and replaces the same values again.
  `fizzbee_process` applies it to every parsed result rather than only to a
  failed one, because a passing run's trace can be saved as evidence too. This
  closes the gap without teaching the text stage to imitate Go's encoder. The
  whole-token rule for short values still applies: a two- to five-character
  value is replaced only where it stands alone, and `retain` withholds a text
  one survives in. The one-character rule is the one D50, D53 and D55 state,
  not the residual D44 recorded.
- **D48, 2026-11-07.** The checker inherits the caller's environment, and
  redaction rather than a reduced environment remains the control. A proposal
  to run the checker under a minimal allowlist would keep credentials it never
  receives out of its output, but the checker is a Go program that reads
  `HOME`, the Go cache and proxy settings and the caller's own configuration; a
  run under an allowlist would either fail or explore a different environment
  from the one a maintainer reproduces by hand, and either outcome would be
  reported as a fault in the model. Redaction is applied where the text leaves
  the process, which is the boundary a test can observe, and D47 closes the
  decoded-value gap that text redaction could not reach.
- **D49, 2026-11-07.** A run is classified before its evidence is redacted.
  `fizzbee_process` parses the checker's own stdout and graph and only then
  applies `redact_result`, and `redact_result` redacts an `Errored` detail as
  well as a counterexample's trace. A replacement is neither injective nor
  type-preserving: replacing first turned `"b": 12` into invalid JSON and could
  make two distinct states compare equal, either of which changes the verdict.
  The parser's refusal quotes the decoded document, so the credential it can
  carry is removed at this stage rather than by the text pass.
- **D50, 2026-11-07.** A one-character credential is replaced only in a value
  position, and a text a credential survives in is withheld. This supersedes
  the one-character residual in D44. `_MIN_SECRET_LENGTH` is 2; below it a
  replacement is anchored after `=`, between quotes, and at a token boundary,
  so a boolean flag cannot rewrite every state count. Values of two characters
  or more keep the whole-token rule from D44 and are carried in
  `Redaction.secrets`; `retain` withholds the text when one survives. The
  escaped spellings also cover Go's `<`, `>`, `&`, U+2028 and U+2029, and the
  `repr` form a parse refusal quotes.
- **D51, 2026-11-07.** The substitution mechanism lives in
  `adapters/fizzbee_replacements`. `Redaction`, `Replacement`, `HIDDEN_VALUE`
  and `WITHHELD` moved there so `fizzbee_redaction` stays inside the 400-line
  limit; which values must be replaced remains in `fizzbee_redaction`, decoded
  traversal in `fizzbee_trace_redaction`, and bounding in `fizzbee_evidence`.
- **D52, 2026-11-07.** A contract names the failures its implementation can
  raise, and documentation names the artefacts that are tracked. `source_text`
  documents `OSError` and `UnicodeDecodeError`, `check` documents a
  specification that cannot be written, and the users' guide and ExecPlan state
  that the seven `models/admission/*/traces/head-*.json` files are tracked
  rather than untracked.
- **D53, 2026-11-07.** A one-character credential is recognized by the label a
  checker prints beside it, and a decoded string that still contains one is
  withheld. D50 protected the character only where an assignment or a quote
  marked it as a value, and a checker prints neither as often as it names the
  variable beside the value: `token x` is an echo too, so the label is matched
  as a position. A decoded string is one value the checker produced, so a
  string that contains the character is withheld rather than recorded. The
  whole text is not withheld on such a character, because a host whose
  environment holds a one-character credential-named boolean would then lose
  every record — the residual D44 recorded — while the string-level rule keeps
  the rest of the trace.

- **D55, 2026-11-07.** A one-character credential is withheld from a decoded
  string whatever its character, and the behavioural scenarios drive the
  command. D53's `character_secrets` held only alphanumeric values, so a
  credential of `#` inside `prefix#` survived a saved trace; every non-empty
  one-character value is now carried, and the cost is bounded to the strings
  that contain that one character. The model-check scenarios called
  `service.run_matrix` directly and asserted the status with `exit_status`,
  which left the `check` command's own wiring untested; the `when` step now runs
  `cli.make_app` and the steps read the report it printed and the status it
  returned, as the drift step already did. The configuration-source contracts
  name the `UnicodeDecodeError` a non-UTF-8 file raises, with a boundary test
  for both queries.

- **D56, 2026-11-07.** Redaction is pinned by properties, and the two count
  comparisons are described separately. A credential is drawn from an alphabet
  whose characters JSON writers spell differently, and every spelling is
  computed in the test rather than read from the adapter, so a needle the
  implementation forgets is a counterexample rather than an agreement between
  two copies of one list. The persisted-record and saved-trace regressions
  parametrize a value mixing an escaped quote with a literal non-ASCII
  character, which no single needle form matches. The fairness scenario asserts
  the command's status, `service`'s docstring names finite or deadlock
  counterexamples and the lasso exception, and the developers' guide separates
  the fixpoint comparison from the matrix consistency check. `check` and
  `drift` are each tested against a `configurations.toml` that is not UTF-8.

- **D57, 2026-11-08.** Evidence is redacted before it is bounded, and the
  scrub is supplied by the caller. Every detail the adapter builds from
  checker-written text is cut to a fixed number of characters from each end,
  and a credential surviving a cut is a fragment no survival check can
  recognize: a value placed across the boundary was recorded as its first few
  characters while the record looked clean. Both ends are kept because the
  checker prints its verdict, a `FAILED:` or `Error:` line, on the last one, so
  a prefix alone would drop the line that explains the failure. `bounded`,
  `with_evidence` and `timeout_detail` therefore take a `Scrub` callable, which
  the process adapter supplies as `Redaction.retain`, and run it while the text
  is still whole. `parse_output` threads the same callable through the five
  details it builds while classification still reads the text exactly as the
  checker printed it, because a replacement is not injective and could change
  the verdict. The assembled detail is retained once more afterwards, and a
  capture the scrub withheld becomes the marker inside the detail rather than
  replacing it, so the diagnosis beside a withheld document survives. An
  exclusion's reason is now refused unless it is a non-empty string rather than
  coerced from whatever TOML held, and the installer removes a partial download
  so a failed `curl` cannot leave a truncated asset that a later run treats as
  installed.

## References

- [RFC 0001: FizzBee model of admission and recovery](rfcs/0001-fizzbee-model-of-admission-and-recovery.md)
- [Developers' guide §11.6](developers-guide.md#116-model-checking)
- [ExecPlan: fizzbee-model-of-admission-and-recovery](execplans/fizzbee-model-of-admission-and-recovery.md)
