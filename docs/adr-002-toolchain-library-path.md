# Architectural decision record (ADR) 002: keep a real toolchain compiler's library directory on the loader path

## Status

Accepted. The adapters mitigate locally and defensively: before they run a real
toolchain `rustc` or `rustdoc` they prefix its own `lib` directory to a
non-empty `LD_LIBRARY_PATH`, as the rustup proxy does. The root cause stays an
upstream defect, and build scripts that Cargo runs without the adapters stay
outside the mitigation.

## Date

2026-10-19.

## Context and problem statement

`cargo kani` and any Kani-gated test cannot compile on this host when the
toolchain Kani drives ships a duplicate `librustc_driver`. The failure surfaces
one layer up, from the `rustversion` build script:

```plaintext
Error: unexpected output from `rustc --version`: ""
```

The report is [leynos/dev-env-rocky#312][issue-312].

### Mechanism

1. `rustc-dev` installs a second copy of the compiler driver in
   `<root>/lib/rustlib/<triple>/lib/`, beside the one in `<root>/lib/`.
2. Both copies declare `RUNPATH $ORIGIN/../lib` and need
   `libLLVM.so.21.1-rust-1.93.0-nightly`. On `nightly-2025-11-21`, `libLLVM`
   lives only in `<root>/lib/`.
3. Cargo gives a build script a `LD_LIBRARY_PATH` that ends with the
   toolchain's `rustc --print target-libdir`, which is the rustlib directory.
4. The loader finds the driver there first. That copy's `$ORIGIN/../lib` is
   `<root>/lib/rustlib/<triple>/`, which has no `libLLVM`, so `rustc` exits
   with status 127 and prints nothing to standard output.
5. `rustversion` reads only standard output, so the failure reads as an empty
   version.

Nothing breaks when `<root>/lib` is also listed in `LD_LIBRARY_PATH`, in either
order: the loader then finds `libLLVM` through the variable.

### Evidence

Re-verified on 2026-10-19 on this host (`rustup 1.29.1`, `cargo 1.99.1`):

- The poisoned probe fails and the controls pass.

  ```plaintext
  LD_LIBRARY_PATH=$RL "$KT/bin/rustc" --version        rc=127
  LD_LIBRARY_PATH=$RL:$KT/lib "$KT/bin/rustc" --version rc=0
  LD_LIBRARY_PATH=$KT/lib:$RL "$KT/bin/rustc" --version rc=0
  "$KT/bin/rustc" --version                             rc=0
  LD_LIBRARY_PATH=$RL "$KT/bin/rustdoc" --version       rc=127
  LD_LIBRARY_PATH=$RL ~/.cargo/bin/rustc +nightly-2025-11-21 --version rc=0
  ```

- The two drivers have the same size and SHA-256, and `libLLVM` is absent from
  the rustlib directory.
- Cargo injects the rustlib directory. A one-crate probe, built by
  `$KT/bin/cargo` with `RUSTC_WRAPPER` set to the installed adapter, printed
  `LD_LIBRARY_PATH=<target>/debug:<target>/debug/deps:<rustlib>` from its build
  script, and `$RUSTC_WRAPPER $RUSTC --version` in that script returned status
  127 with empty standard output. The source is `Compilation::fill_env` in
  Cargo's `src/compiler/compilation.rs`, reached for build scripts through
  `host_process`; it pushes `sysroot_target_libdir`.
- The adapter is on the failing path. A build script is not inside an admitted
  scope, so `$RUSTC_WRAPPER $RUSTC --version` goes through admission, and the
  supervisor launches the real `rustc` with the request's environment unchanged.

One claim in the issue needs narrowing. Seven installed toolchains hold the
duplicate driver, but only `nightly-2025-11-21` fails the probe on this host.
The other six also keep a `libLLVM` in their rustlib directory, so the
duplicate finds its dependency. The duplicate is necessary for the failure and
not sufficient.

### Ownership

- This repository does not install Kani or `rustc-dev`, and it does not set
  `LD_LIBRARY_PATH`. Kani 0.67.0 pins `nightly-2025-11-21`.
- Cargo owns the build-script environment and rustup owns the proxy's prepend.
- Guards that match only loader messages, such as the `theoremc`
  `is_unusable_kani_environment` check, do not see this failure. That belongs to
  `theoremc`.

## Decision drivers

- Every agent that meets the symptom re-diagnoses it from scratch.
- The adapter forwards the caller's environment verbatim, so it is the one
  place on the path that can make an admitted launch independent of the
  inherited loader path.
- The change must not alter argument vectors, sccache stripping, the
  `clippy-driver` contract or any cache key.

## Options considered

| Option                                   | Effect                                                             | Verdict  |
| ---------------------------------------- | ------------------------------------------------------------------ | -------- |
| Record the defect and defer it upstream  | No code change; the failure remains for every admitted launch      | Rejected |
| Normalize in the supervisor's launch     | Misses the already-admitted `execvp` path; needs the request's cwd | Rejected |
| Normalize in the adapter                 | One computation covers the direct exec and the admitted request    | Accepted |
| Prepend unconditionally, as rustup does  | Adds the variable to clean environments for no benefit             | Rejected |
| Run `rustc --print sysroot` to find root | Fails under the poisoned path, which is the case being repaired    | Rejected |

_Table 1: Options for the mitigation._

## Decision outcome

Treat the collision as an upstream defect among rustup, Cargo and `rustc-dev`,
and mitigate it locally in the adapter.

`buildlimits.toolchain_env.toolchain_environment(argv, env, cwd)` is a pure,
filesystem-only function. It returns a new environment, and `adapter._main`
applies it once, before the already-admitted check, so the direct `execvpe` and
the `CompileRequest` carry the same environment.

- It recognizes a real toolchain compiler only when the command, after any
  leading `sccache` layers, resolves (as `execvpe` would, in the request's
  directory, following symbolic links) to `<root>/bin/rustc` or
  `<root>/bin/rustdoc`, and `<root>/lib` holds a `librustc_driver-*.so`. The
  rustup proxy resolves to `rustup`, so it is not recognized; nor is
  `clippy-driver`, an unresolvable name or a layout without a driver.
- For a recognized compiler it prefixes `<root>/lib:` to a non-empty
  `LD_LIBRARY_PATH` that does not already list `<root>/lib` (an entry counts
  only if it opens as that directory). The original value follows verbatim. An
  unset or empty variable stays as it is. Applying the function twice equals
  applying it once.
- It never runs the compiler and never changes `argv`.

### What it mirrors, and where it differs

rustup's `Toolchain::set_ldpath` (1.29.1, `src/toolchain.rs`) calls
`env_var::insert_path("LD_LIBRARY_PATH", [<toolchain>/lib], None, ...)`. That
function splits the existing value, prepends each directory unless the list
already contains it anywhere, and joins the result. It sets the variable even
when it was unset. On macOS it uses `DYLD_FALLBACK_LIBRARY_PATH` and restores
the default fallback directories; this package supports Linux only.

The adapter follows the dedupe rule and the prepend position. It deliberately
leaves an unset or empty variable alone: an empty path cannot shadow `RUNPATH`,
and adding a loader entry to a clean environment would change what the
compiler's children (linkers, proc-macro hosts) inherit for no gain.

### Why sccache and kache are unaffected

sccache 0.14.0 hashes the compilation from the source files, the dependency
information's `# env-dep:` variables and the variables beginning `CARGO_`
(`generate_hash_key` in `src/compiler/rust.rs`), plus the digests of the
libraries in the compiler's sysroot. `LD_LIBRARY_PATH` is not an input. Its
server caches compiler detection by executable path and modification time, not
by environment. The forwarded environment reaches the server's `rustc` probes
(`--print=sysroot`), where the repaired path helps rather than splits the cache.

kache is unaffected for the same reasons, and one more. Its key is built from
the compilation and the environment it hashes, none of which this repair
touches; and because kache wraps the compiler inside the group's own scope
([ADR 004](adr-004-kache-cache-backend.md)), the repaired path reaches exactly
the process that composes the cache key, with no server-side probe in between.

## Consequences

- An admitted launch of a recognized real `rustc` or `rustdoc` no longer
  depends on an inherited poisoned path. This repairs the reported
  `rustversion` case when the build script runs
  `$RUSTC_WRAPPER $RUSTC --version`, which the one-crate probe confirms.
- The adapter cannot change the environment of build scripts that Cargo
  launches, nor a build script that runs `$RUSTC --version` without the
  wrapper. Those need the documented workaround in the user's guide.
- Processes that are already running keep their old environment.
- Operators take no action and no migration document is needed.
- The helper adds one directory scan and one path resolution to each adapter
  invocation.

## Known risks and limitations

- Recognition depends on the `bin/` and `lib/` layout that rustup installs. A
  differently laid-out toolchain is left alone, which is the safe failure.
- The helper fails open, and each kind of filesystem trouble is read as the
  answer that does the least harm. Failed toolchain recognition leaves the
  environment unchanged. A `PATH` directory that cannot be searched is skipped,
  and a later match is repaired as usual. A loader-path entry that does not
  open does not count as `<root>/lib`, so a recognized toolchain still gets the
  prefix unless another entry identifies it. Raising from a defensive repair
  would stop a build that works today, and the compiler's own error remains the
  diagnostic.
- A toolchain directory whose path contains `:` cannot be expressed in the
  variable and is left alone.
- The version boundaries of the upstream behaviour are not stated here: only
  the toolchains in the evidence were observed.

[issue-312]: https://github.com/leynos/dev-env-rocky/issues/312
