# Keep a real toolchain compiler's library directory on the loader path

This ExecPlan is a living document. Update Progress, Surprises and discoveries,
Decision log and Outcomes and retrospective as work proceeds.

Status: IN PROGRESS (implementation complete; in review). The supervising
session approved the scope in the assignment for
[leynos/dev-env-rocky#312][issue-312]; the pull request is not to be merged
until that session says so.

## Purpose and big picture

A build script that runs `$RUSTC_WRAPPER $RUSTC --version` fails with
``unexpected output from `rustc --version`: ""`` when `$RUSTC` is the real
`rustc` of a toolchain whose `rustc-dev` duplicated `librustc_driver` into its
rustlib directory, and Cargo has put that directory on `LD_LIBRARY_PATH`. The
compiler exits 127 for want of `libLLVM`. After this change the adapters prefix
the toolchain's own `lib` directory for such a compiler, as the rustup proxy
does, and the documentation lets an agent recognize the symptom and apply the
workaround for callers that bypass admission.

Observable result: the one-crate probe in the Evidence section prints
`RC=Some(0)` through the new adapter where the installed adapter prints
`RC=Some(127)`.

## Constraints

- Implement in this repository only; do not edit dev-env-rocky.
- Never bypass compile admission. Version probes of a real compiler are not
  compiles.
- The helper is pure and filesystem-only: it never runs a compiler, never
  changes `argv`, and leaves the rustup proxy, `clippy-driver` and unrecognized
  compilers alone.
- No file over 400 lines; en-GB-oxendict spelling; no build targets in `/tmp`.

## Tolerances (exception triggers)

- Any change to `groups.py`, `launch.py`, the protocol or the supervisor.
- Any observed effect on sccache keys.
- More than two gate failures that are not explained by a cited residual.

## Risks

- Recognition could miss a differently laid-out toolchain. Mitigation: it fails
  safe, leaving the environment unchanged; documented in the ADR.
- The end-to-end test depends on host toolchains. Mitigation: it probes for one
  that really fails and skips cleanly.

## Evidence (investigation, 2026-10-19)

1. The probe reproduces on `nightly-2025-11-21`: poisoned `rustc --version`
   exits 127; with `$KT/lib` listed before or after the rustlib directory it
   exits 0; the rustup proxy exits 0; `rustdoc` fails the same way.
2. Only one of the seven toolchains with a duplicate driver fails here. The
   other six keep a `libLLVM` in rustlib. The issue's scope table overstates
   the affected set.
3. Adapter trace: a build script is not in an admitted scope, so the request
   goes through admission and `launch.py` runs
   `os.execvpe(argv[0], argv, spec.env)` with the request environment verbatim.
   The adapter has no notion of the proxy; `rustc` resolving to rustup is the
   only distinction available.
4. rustup 1.29.1 `Toolchain::set_ldpath` calls `env_var::insert_path` with
   `<toolchain>/lib`: unconditional set, prepend unless already listed
   anywhere, exact path comparison, `DYLD_FALLBACK_LIBRARY_PATH` on macOS only.
5. sccache 0.14.0 `generate_hash_key` hashes sources, dep-info `env-dep`
   variables and `CARGO_*` variables only; `LD_LIBRARY_PATH` is not an input.
6. Cargo `Compilation::fill_env` (`src/compiler/compilation.rs`) adds
   `sysroot_target_libdir` for build scripts (`host_process`). Re-confirmed
   with a one-crate build through the installed adapter: the build script saw
   `...target/debug:...target/debug/deps:<rustlib>` and its wrapped `--version`
   returned 127. With the new adapter the same crate returns 0.

Raw outputs are in the session scratchpad (`probe.out`, `cargo-probe.out`,
`cargo-probe-fixed.out`, `red.out`).

## Verification plan

Invariants of `toolchain_environment(argv, env, cwd)`:

1. Purity: the input mapping and argv are unchanged, a new mapping is returned.
   Method: parameterized unit test.
2. Idempotence: applying it twice equals applying it once. Method: Hypothesis
   property over generated search paths (`lib`, `rustlib`, other, empty and
   relative entries). Non-vacuity: `event` classification of already-present
   and empty cases, explicit `@example`s, and seeded faults (removing the
   dedupe, appending instead of prepending, dropping the non-empty guard,
   dropping the `bin` check) each fail named tests.
3. Ordering: for a recognized compiler and a non-empty path lacking the lib, the
   result is `<lib>:<original>` with the original verbatim, including empty
   segments, duplicates and missing entries. Same property and units.
4. Conservatism: the proxy, `clippy-driver`, layouts without a driver, missing
   commands, and unset or empty variables yield an unchanged copy.
5. Single application: the direct exec and the `CompileRequest` receive the same
   environment. Method: adapter-level tests with a fake supervisor and a faked
   `os.execvpe`.

Axioms: the glibc loader searches `LD_LIBRARY_PATH` before `RUNPATH` and finds
`libLLVM` through any listed directory (probe 2 and its reverse order);
rustup's and sccache's behaviour is as read from the cited sources.

## Progress

- [x] (2026-10-19) Reproduced the probe; traced the adapter; read rustup,
  Cargo and sccache sources; re-confirmed the Cargo injection.
- [x] (2026-10-19) Red: `tests/test_toolchain_library_path.py` fails for the
  expected reason (`red.out`).
- [x] (2026-10-19) Green: `buildlimits/toolchain_env.py` and the `adapter._main`
  wiring; unit, property and real-toolchain tests.
- [x] (2026-10-19) ADR 002, user's guide, developer's guide, contents.
- [x] (2026-10-19) Gates through scrutineer. Round 1 (800bc6a) found five lint
  findings; round 2 (6b96af1) found a duplicated test fixture and a spelling
  error; round 3 on 89505f1 passed all nine gates (`make test` 1738 passed, 22
  skipped; `make test-systemd-user` 22 passed).
- [ ] Pull request, review loop, mergeable.

## Surprises and discoveries

- Only one of seven duplicate-driver toolchains actually fails (see Evidence 2).
  The end-to-end test therefore probes for a failing toolchain instead of
  assuming a duplicate means failure.
- The failure is reachable through the adapter, contrary to the first
  assessment that reachability was unresolved: the wrapped `--version` in the
  build script is an admitted launch with the poisoned environment.
- The fix is adapter-side only, so it works against the already-running
  supervisor: no restart is needed to benefit.

## Decision log

- Local mitigation, adapter side, in `_main`, rather than in `launch_spec`:
  covers the direct-exec path and resolves the executable in the caller's own
  directory and `PATH`. (ADR 002.)
- Deviate from rustup on an unset or empty variable: leave it alone. Rationale
  in ADR 002.
- `run()` takes one `Invocation` (argv, cacheable, env) instead of gaining a
  fifth parameter, which the repository's four-argument lint limit forbids; its
  `env` defaults to an `os.environ` snapshot, so there is no second snapshot.
  Sixteen test call sites changed mechanically.
- Presence is judged by real path, which is wider than rustup's exact
  comparison and safe because the loader resolves entries the same way.

## Conformance basis

No Terms of Reference or technical design applies. Governing records: ADR 002
(this change), ADR 001 (graceful restart, unchanged), the upstream issue
[leynos/dev-env-rocky#312][issue-312].

## Outcomes and retrospective

To be completed when the pull request is mergeable.

[issue-312]: https://github.com/leynos/dev-env-rocky/issues/312
