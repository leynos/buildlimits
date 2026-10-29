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

An existing `sccache` configuration needs no change: `cache_mode` defaults to
`sccache`, and the sccache settings, status wording and metrics are the same as
before. A host that wants kache instead sets `cache_mode = "kache"`, installs
kache itself, names the socket of a daemon it runs as a separate unit in
`kache_socket`, and leaves the daemon running -- the supervisor never starts,
stops or owns one, and provisioned hosts do not get kache from
`devhost.buildlimits`. The socket key is required in `kache` mode, so a
configuration that omits it is rejected rather than left to resolve a default.
See
[Choose a cache backend](users-guide.md#choose-a-cache-backend).

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

No change to `supervisor.toml` or the Cargo configuration is needed for this
step. The supervisor unit should also carry `KillMode=mixed`, which the role
must render; see
[Units the restart relies on](users-guide.md#units-the-restart-relies-on).
Until it does, a restart that lands while a build is being placed is retried
rather than lost, but the unit still signals the supervisor's helper commands.

## Restart safely

This restart is performed by the supervisor you are replacing, and the
dev-env-rocky copy is not graceful for builds in flight. On stop, it marks
admission unhealthy, so queued compile requests are refused, and it terminates
the compiler groups that are running. Builds that were waiting or compiling
fail. The new supervisor's start-up also stops a managed cache that the old one
left running, so a live cache no longer ends the restart in an operator-action
exit. A cache that is between automatic restarts at that moment still needs one
`systemctl --user start` once it has settled.

Pause new builds before the final status check, so that none can start between
the check and the restart: restart only when `build-limits-status` reports
`held 0` and `no requests waiting` and nothing is about to submit. This first
restart cannot be made disruption-free while builds may start, because the old
supervisor cancels whatever it holds. If builds cannot be paused, wait for the
active ones to finish, or accept that those started in the gap will be
cancelled. Once this release is running, later upgrades need no such care: the
[user's guide](users-guide.md#restart-the-supervisor-after-an-upgrade)
describes the restart that waits for running builds and requeues waiting ones.

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
