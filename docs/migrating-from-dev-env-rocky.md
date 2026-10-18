# Migrating from the dev-env-rocky copy

This guide is for operators who run the compile-admission package as installed
by the `devhost.buildlimits` Ansible collection in
[dev-env-rocky](https://github.com/leynos/dev-env-rocky), where it lived as
`python/build_limits`. From release 0.1.0 the package is developed and
installed from this standalone repository. Behaviour, configuration and console
scripts carry over; the changes that need action are listed below.

## What changed

| Area                | dev-env-rocky copy       | buildlimits 0.1.0                      |
| ------------------- | ------------------------ | -------------------------------------- |
| Source location     | `python/build_limits`    | this repository, flat `buildlimits/`   |
| Import package name | `build_limits`           | `buildlimits`                          |
| Python requirement  | 3.12 or newer            | 3.14 or newer                          |
| Installation        | staged by the collection | `uv tool install` from this repository |

_Table 1: Differences between the dev-env-rocky copy and this repository._

These are unchanged:

- The six console scripts: `build-limits-rustc`, `build-limits-rustdoc`,
  `build-limits-supervisor`, `build-limits-status`, `build-limits-recover` and
  `build-limits-metrics`. Systemd units and Cargo's `rustc-wrapper` and
  `rustdoc` settings that name them keep working.
- The `~/.config/buildlimits/supervisor.toml` file and its keys, and the
  `BUILD_LIMITS_*` environment variables, as listed under
  [Configuration](users-guide.md#configuration) in the user's guide.
- The `buildlimits-supervisor.service` and `buildlimits-sccache.service`
  user units, the `buildlimits-compilers.slice` slice, and the runtime paths
  under `$XDG_RUNTIME_DIR/buildlimits`.

## Who is affected

- Operators who install or upgrade the package on a host.
- Anyone who imports the module by name in scripts or tooling. The import name
  is now `buildlimits`; update any `import build_limits` or
  `from build_limits ...` statements. Operators who only use the console
  scripts are unaffected.
- Anyone who used the placeholder API of this repository's initial skeleton.
  Its `hello` function and the optional `rust` extension module it loaded are
  gone. The package's top level now exports the admission types listed in
  `buildlimits.__all__` instead.
- Maintainers of the `devhost.buildlimits` collection, who must retarget the
  role (see [Required collection change](#required-collection-change)).

## Migration steps

1. Confirm that Python 3.14 is available. The previous copy ran on 3.12 or
   newer, so a host provisioned for it may not have 3.14. The `--python 3.14`
   flag below lets `uv` fetch an interpreter where policy allows it.

2. Install the package from this repository:

   ```bash
   uv tool install --python 3.14 git+https://github.com/leynos/buildlimits
   ```

3. Update any code that imports `build_limits` to import `buildlimits`.

4. Check the build state before restarting the supervisor, as described in
   [Restart safely](#restart-safely), then restart it so that the new code is
   loaded:

   ```bash
   build-limits-status
   systemctl --user restart buildlimits-supervisor.service
   ```

5. Verify the result, as described in [Verification](#verification).

No change to `supervisor.toml`, the Cargo configuration or the unit files is
needed for this step.

## Restart safely

A supervisor restart is not graceful for builds in flight. On stop, the
supervisor marks admission unhealthy, so queued compile requests are refused,
and it terminates the compiler groups that are running. Builds that were
waiting or compiling fail.

Restart only when `build-limits-status` reports `held 0` and
`no requests waiting`. Otherwise, wait for the builds to finish, or accept that
they will be cancelled.

## Verification

Run `build-limits-status` after the restart:

```bash
build-limits-status
```

```plaintext
capacity 2, held 0, healthy, caching through sccache
```

The line should report the expected capacity and `healthy`. Then run a small
`cargo build` and confirm that it completes. The [user's guide](users-guide.md)
describes the remaining status output and how to read the journal if the
supervisor does not report healthy.

## Required collection change

The `devhost.buildlimits` role in dev-env-rocky still stages
`src/build_limits/*.py` from its `admission_package_source` setting. That
layout does not match this repository, where the package is the flat
`buildlimits/` directory. The collection's maintainers must retarget the role
to this layout before it can install from here. This guide does not record that
change as done; until it is, install the package with the command above and
leave the collection to provision the pool, units and Cargo handover.

[Back to the documentation contents](contents.md)
