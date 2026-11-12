# RFC 0003: Exempting safe rustc queries from compile admission

## Preamble

- **RFC number:** 0003
- **Status:** Accepted
- **Created:** 2026-10-25
- **Scope:** Issue #27's fail-closed exemption for rustc toolchain queries.

Issue #27 refers to `docs/rfc-concurrent-build-limits.md`, which does not exist
in this repository. This RFC is the indexed replacement for that path. It
records the lock-ordering hazard, the exemption contract, source findings and
the available host evidence.

## Summary

Cargo can hold its host-wide `.package-cache` lock while it starts a rustc
information query. If `build-limits-rustc` queues that query for a compile
token, other Cargo processes that need the lock wait behind a request that is
itself waiting in the compile queue. This RFC proposes running only
source-backed, query-only rustc invocations without admission. It covers direct
rustc and verified Clippy and Dylint workspace-driver queries. Real compiles
and every uncertain invocation remain admitted.

The adapter records each exemption as an outcome-only `exempt` event. The query
uses neither a compile token nor a caller sccache server. The policy stays
closed to unreviewed flags, wrappers and query forms.

## Problem

Compile admission is a fair queue for compiler work. Fairness does not resolve
a lock-ordering cycle when a Cargo process holds a host-wide lock while its
compiler child waits in that queue. Other Cargo commands can then block on the
lock, including commands in unrelated repositories and worktrees. A single
build can issue multiple probes, so a long queue multiplies the time for which
Cargo's lock waiters are stalled.

A caller holding a host-wide lock must not wait in compile admission for a
query that cannot release that lock. The same ordering rule applies to other
shared locks that may be held by Cargo, including registry or Git database
locks, even though this incident establishes only the package-cache case.

The queue was unusually long during the soft-lockup incident tracked by
[#328](https://github.com/leynos/buildlimits/issues/328). The earlier
manager/scope failure tracked by
[#313](https://github.com/leynos/buildlimits/issues/313) is related host
history, but is not evidence for this lock-ordering mechanism.

## Evidence and uncertainty

The categories below distinguish reported observations, source review and
reproduction. A report is not promoted to a source finding, and a successful
acceptance run does not rewrite the earlier incident record.

### Reported incident: 24 October 2026

The issue report attributes the following observation to `rohga.df12.net` on
24 October 2026:

- `cargo build`, PID `1191072`, was started 62 minutes earlier by the theoremc
  test `theorem_file_macro_bdd`, using temporary crate `/tmp/.tmpwzST01`.
- The process held an exclusive `FLOCK ADVISORY WRITE` lock on
  `~/.cargo/.package-cache`, inode `64763336`.
- Its only child was `build-limits-rustc` running
  `nightly-2026-01-30/.../bin/rustc -vV`. `build-limits-status` reported that
  child waiting for 58.4 seconds for a compile slot while all four slots were
  held.
- `/proc/locks` listed 62 further processes blocked on the same lock; their
  wait channel was `locks_lock_inode_wait`. One was `cargo metadata`, launched
  by `cargo-dylint` in an unrelated netsuke gate, session `7d2cdc6e`, blocked
  for more than five minutes.

These facts establish that the package-cache lock was held during a queued
`-vV` query. They do not establish the full lock interval or whether every
Cargo probe occurs inside it.

### Additional review report: 25 October 2026, uncorroborated

An additional review supplied a `sysdoctor` snapshot from `rohga.df12.net` on
25 October. The underlying snapshot was not independently retained or
corroborated, so the following remain attributed to that review report:

- The snapshot listed one exclusive `.package-cache` holder, PID `4124239`,
  seven kernel-listed waiters, and 19 shared `.package-cache-mutate` holders.
  The holder was a femtologging `cargo check` in `rust_extension`, using
  `nightly-2026-05-28`, and was 2 minutes 21 seconds old. It had no sockets
  open.
- Its only child was a queued Dylint version probe:
  `build-limits-rustc ~/.dylint_drivers/nightly-2026-05-28/dylint-driver
  ~/.rustup/toolchains/nightly-2026-05-28-x86_64-unknown-linux-gnu/bin/rustc
  -vV`.
  The request had waited 22.5 seconds; the supervisor listed 116 waiting
  requests.
- A later holder was an unrelated netsuke Cargo run in a `target/.tmp*`
  directory whose only child was plain `rustc -vV`, queued for 27 seconds. The
  lock passed to another waiter rather than becoming available when the first
  holder exited.
- A sample of about ten queued probes included `rustc -vV`, `rustc --version`,
  `rustc --version --verbose`, and Cargo's stdin `--print=file-names` query.
  The latter included `--target wasm32-wasip2` and appended flags such as
  `-D warnings`, `-Zthreads=8` and `-Clink-arg=-fuse-ld=mold`.
- A separate operator observation from the preceding day said a Whitaker
  `cargo check` held the lock for about 19 minutes while a fixture's
  `cargo clean` waited about 12.5 minutes. That holder had exited before it
  could be inspected, so its child and exact lock sequence were unknown.
- The report inferred that Clippy wrappers could cause the same cycle, but it
  had not captured a live Clippy probe. The later host acceptance below is
  separate evidence for Clippy query execution.

### Pinned-source findings

The classifier's source review is pinned to the following upstream revisions:

- Rust compiler and Clippy:
  [`a293cc4af8b26701c42738381c0c6f9d2ba881e0`][rust-commit].
  [`rustc_driver_impl::print_crate_info`][rust-driver] stops after ordinary
  information queries; `NativeStaticLibs` and `LinkArgs` are special because
  they need compilation/linking. The [Clippy driver][clippy-driver] recognizes
  wrapper mode when the inner executable is `rustc`. It classifies `-vV` and
  supported `--print` queries, except `crate-root-lint-levels`, as information
  queries and invokes `rustc_driver` without enabling Clippy lints.
- Cargo:
  [`fe2f314aef06e688a9517da1ac0577bb1854d01f`][cargo-commit],
  [`target_info.rs`][cargo-target-info]. The target-information query uses
  stdin, the synthetic crate name `___`, `--print=file-names`, crate types,
  target information and configured rustflags. The source establishes the
  query's purpose; it does not prove that arbitrary appended flags are harmless.
- Dylint:
  [`9adfa398661273ca7dc99df9bf2c26ae6f61b1c5`][dylint-commit],
  [`driver/src/lib.rs`][dylint-driver]. Its `-V` branch prints the driver
  version. Other calls construct rustc arguments from `DYLINT_RUSTFLAGS` and
  library paths before invoking `rustc_driver`. This is why non-whitespace
  `DYLINT_RUSTFLAGS` fails closed. Effects of loading a real Dylint library for
  an information query remain unverified.
- sccache:
  [v0.18.0 `commands.rs`][sccache-command] starts or connects to the server for
  a `Compile` command before sending the compile request
  ([dispatch][sccache-dispatch]). The client therefore can wait for its server
  before the compiler decides that an invocation is query-only.

These source findings support the narrow grammar; they do not establish the
exact interval for which Cargo holds `.package-cache` around a probe.

[rust-commit]: https://github.com/rust-lang/rust/tree/a293cc4af8b26701c42738381c0c6f9d2ba881e0
[rust-driver]: https://github.com/rust-lang/rust/blob/a293cc4af8b26701c42738381c0c6f9d2ba881e0/compiler/rustc_driver_impl/src/lib.rs#L633-L871
[clippy-driver]: https://github.com/rust-lang/rust/blob/a293cc4af8b26701c42738381c0c6f9d2ba881e0/src/tools/clippy/src/driver.rs#L238-L313
[cargo-commit]: https://github.com/rust-lang/cargo/tree/fe2f314aef06e688a9517da1ac0577bb1854d01f
[cargo-target-info]: https://github.com/rust-lang/cargo/blob/fe2f314aef06e688a9517da1ac0577bb1854d01f/src/cargo/core/compiler/build_context/target_info.rs#L181-L215
[dylint-commit]: https://github.com/trailofbits/dylint/tree/9adfa398661273ca7dc99df9bf2c26ae6f61b1c5
[dylint-driver]: https://github.com/trailofbits/dylint/blob/9adfa398661273ca7dc99df9bf2c26ae6f61b1c5/driver/src/lib.rs#L397-L445
[sccache-command]: https://github.com/mozilla/sccache/blob/v0.18.0/src/commands.rs#L315-L342
[sccache-dispatch]: https://github.com/mozilla/sccache/blob/v0.18.0/src/commands.rs#L894-L950

### Host acceptance: 25 October 2026

The host acceptance ran on `rohga.df12.net` on 25 October 2026. It used private
Cargo homes, fresh target directories, a held supervisor pool and a timing shim
around the sibling adapter. The tests are:

- [Systemd held-pool checks](../../tests/test_rustc_query_systemd.py)
- [Host Cargo checks](../../tests/test_rustc_query_host.py)
- [Query timings](../../tests/test_rustc_query_timings.py)

Cargo was `1.99.1`, commit `5f94df4789f005f9a352888e8355ffc645b7ed0e`. Rustc was
`1.99.1`, commit `58cf8f9c35b355199727627d07ae34a4e2846d67`, with LLVM
`23.1.1`. Clippy and `clippy-driver` were `0.1.99` from commit prefix
`58cf8f9c35`.

| Scenario                                                            | Result while the pool was held                                                                           | Probe timings and lock evidence                                                                                                                   |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cold `cargo metadata --offline --format-version 1` in a fresh crate | Status 0 in 0.315711 s; every slot remained held and the queue stayed empty                              | `rustc -vV` 0.082321 s; Cargo target information 0.092130 s; no package-cache lock-wait lines                                                     |
| `cargo clippy --offline` in a fresh crate                           | Status 0 in 0.997547 s; its real compilation queued exactly once and had no shim end time before release | Driver probes completed in 0.085750 s, 0.088553 s and 0.079477 s; the shim recorded `RUSTC_WORKSPACE_WRAPPER=.../clippy-driver`                   |
| Concurrent `cargo metadata` while that Clippy compile was queued    | Status 0 in 0.264717 s, before the compile barrier was released                                          | No package-cache lock-wait lines; metadata probes completed while the real compile remained queued                                                |
| Dylint readiness                                                    | `cargo-dylint --version` returned `6.0.1`                                                                | Real Dylint resolution and timing were skipped because caller-supplied `DYLINT_LIBRARY_PATH` was unset; the fake-driver held-pool scenario passed |

The following five samples are direct-compiler baselines from
`tests/test_rustc_query_timings.py::_run_probe`; they invoke rustc and its
workspace drivers without `build-limits-rustc`. All returned status 0 while
every pool slot remained held, so they bound query cost only. The held-pool
systemd test in `tests/test_rustc_query_systemd.py` establishes that the
adapter dispatches these queries without a token. One warm-up preceded each
set. Every value is in seconds.

| Query                                  | Five samples                                     |
| -------------------------------------- | ------------------------------------------------ |
| rustc `-vV`                            | 0.011667, 0.011898, 0.010525, 0.011451, 0.012003 |
| rustc Cargo target information         | 0.013859, 0.013443, 0.014551, 0.014144, 0.014336 |
| Clippy driver `-vV`                    | 0.016781, 0.016298, 0.015727, 0.016614, 0.015664 |
| Clippy driver Cargo target information | 0.018653, 0.020448, 0.023874, 0.021662, 0.020693 |

The concurrent metadata run emitted no package-cache lock-wait lines while a
real Clippy compilation was queued. This is a successful observation for that
run: the recorded timestamped lock-wait set was empty. It is not proof that
Cargo never holds the lock across a queued real compile.

## Goals and non-goals

- Goals:
  - Keep exact, non-compiling rustc information queries out of the fair compile
    queue and away from caller sccache waits.
  - Recognize only source-backed compiler and workspace-driver identities.
  - Make exemptions visible as outcome-only adapter telemetry.
  - Preserve normal token admission for compiles and uncertain arguments.
  - Record the lock-ordering hazard and distinguish evidence from open
    explanations.
- Non-goals:
  - Exempting general rustc flags, rustdoc queries or arbitrary wrappers.
  - Changing the compile queue's fairness, token ownership or sccache
    composition for actual compiles.
  - Inferring safety from a parent process's lock state.

## Proposed design

### Fail-closed query grammar

The classifier applies only to the rustc adapter entry point, after the
already-admitted-scope check. It accepts these complete argument forms:

- One version request: `-V`, `--version` or `-vV`, optionally followed by one
  `--verbose`.
- One or more no-input `--print=KIND` requests from the closed set `sysroot`,
  `target-libdir`, `cfg`, `target-list`, `target-cpus` and `target-features`.
  Separate-value `--print KIND` is allowed only for direct rustc.
- Cargo's stdin target-information query: input `-`, crate name `___`, at least
  one allow-listed crate type, `--print=file-names`, and only the recognized
  print, target and warning tokens. Singleton fields and repeated print or
  crate-type values are rejected.

Argument files (`@path`), input files, output redirection, `--emit`,
`native-static-libs`, unsupported print kinds and unconsumed tokens stay
admitted. The parser accepts OS argv bytes but catches decoding and path
resolution failures; classification returns `False` rather than raising.

### Compiler and wrapper identity

After removing leading caller `sccache` layers for inspection, the compiler is
either direct `rustc` or exactly one `clippy-driver` or `dylint-driver`
followed by an inner executable whose basename is `rustc`. No other wrapper,
nested driver, `cargo-clippy` or `sccache` after a driver is recognized.

When `RUSTC_WORKSPACE_WRAPPER` is present, the configured wrapper and invoked
driver are resolved with `toolchain_env.locate_executable`, relative to the
provided environment and working directory. Symlinks are followed and both
paths must identify the same file. An absent, malformed, unresolved or
mismatched value fails closed. Only when the variable is absent does the closed
`clippy-driver`/`dylint-driver` basename allow-list establish identity. The
filesystem lookup is isolated to this wrapper verification.

For Dylint, any non-whitespace `DYLINT_RUSTFLAGS` keeps the request admitted.
The driver may append those tokens after classification. The wrapper itself and
inner rustc remain in the exempt exec vector.

### Dispatch, execution and telemetry

`adapter._main` computes `cwd` and the effective environment once. After the
admitted-scope check, only `rustc_main` enables the opt-in query classifier;
`rustdoc_main` passes `False`. A recognized query records an `adapter_finished`
event with outcome `exempt`, then execs `groups.compiler_argv(argv)` with the
same effective environment. It does not contact the supervisor, request a token
or emit a second summary if exec fails. The existing diagnostic prefix and
failure status are preserved.

`compiler_argv` removes the caller's leading sccache wrapper from this direct
query exec. In sccache v0.18.0, a `Compile` command establishes or starts its
server before it submits compiler work, so retaining that wrapper could wait
before a query reaches rustc. Supervisor-composed sccache is applied only after
admission by `groups.launch_spec`; the exempt path cannot enter that branch.

The `exempt` event has no wait or runtime fields. It increases the bounded
outcome count but contributes no samples to admission wait or runtime
distributions. The adapter suppresses telemetry failures; owned journal sinks
are closed after emission, while injected sinks remain open.

## RUSTFLAGS review question

Pinned source review confirmed that `-D warnings`, `-Zthreads=8`,
`-C linker=PATH` and `-C link-arg=VALUE` do not change rustc's
`Compilation::Stop` result for the reviewed information queries. This does not
expand the classifier's accepted grammar: unsupported tokens still queue.

The effects of arbitrary flags, `--emit`, argument-file contents, print
redirection and loaded Dylint-library callbacks remain unverified. Keep the
current admission policy until any proposed subset has separate source review,
tests and measurements.

## Residual hazards and deferred work

- The package-cache lock's full acquisition and release span around Cargo
  probes remains unknown. A parent-held or inherited lock is possible but
  unconfirmed. The host acceptance observed no lock-wait lines in the metadata
  scenarios; it cannot establish global absence of such waits.
- Other host-wide locks, including possible registry or Git database locks,
  could produce the same ordering hazard. They have not been established by
  this evidence.
- Real compiles and unsupported or malformed queries still wait in admission.
  A lock holder that starts one of those invocations can still delay other lock
  users; the query exception does not solve that general case.
- A short-path or priority lane for a parent holding `.package-cache` is
  deferred. Reading `/proc/locks` to identify the parent is racy and adds
  expensive process inspection. The query exemption directly removes the
  confirmed probe form from admission.
- Additional workspace wrappers, wider print-query targets, reviewed
  `RUSTFLAGS` subsets and rustdoc queries are deferred until each has pinned
  semantics and held-pool measurements. Rustdoc uses a separate adapter and is
  not part of Cargo's rustc query grammar.
- Real Dylint host resolution and timings still need an environment that
  supplies a prebuilt library through `DYLINT_LIBRARY_PATH`. The fake-driver
  test proves dispatch and queue behaviour, not the effect of a loaded Dylint
  library.

## Recommendation

Keep the narrow direct-rustc, Clippy and Dylint query allow-list and the
outcome-only telemetry. Leave the lock-span and Dylint-library questions
explicitly open. Do not add a priority lane or broaden wrapper, print-query or
flag recognition until new evidence is reviewed.
