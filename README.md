# 🚦 buildlimits

*Machine-wide admission for concurrent Cargo compilation: one shared budget,
every build in the queue.*

buildlimits caps how many Cargo compiler groups run at once across a whole
machine, for every agent, repository and worktree, without changing a single
build command. A guildmaster token pool holds the budget; a small per-user
supervisor hands out tokens and takes them back only when a compiler group has
truly finished.

______________________________________________________________________

## Why buildlimits?

Ten agents running `cargo test` in ten worktrees will happily start a hundred
`rustc` processes and push the host into swap. Cargo's `--jobs` only governs
one build at a time.

- **One budget for the whole host**: compile slots are shared across every
  user process, not negotiated build by build.
- **Nothing to rewrite**: Cargo already calls `RUSTC_WRAPPER` and `RUSTDOC`;
  buildlimits slots in there.
- **Honest accounting**: a token returns only once every process in its
  compiler scope has exited, including work detached into sccache.
- **Visible queueing**: waiting builds say so, and `build-limits-status` shows
  who holds what.

______________________________________________________________________

## Quick start

### Installation

buildlimits needs Python 3.14, a systemd user manager and a guildmaster pool at
`/dev/guild`.

```bash
uv tool install --python 3.14 git+https://github.com/leynos/buildlimits
```

On managed hosts, the `devhost.buildlimits` collection in
[dev-env-rocky](https://github.com/leynos/dev-env-rocky) provisions the pool,
the user units and the wrapper handover for you. If you ran the earlier copy
from dev-env-rocky, see
[Migrating from the dev-env-rocky copy](docs/migrating-from-dev-env-rocky.md).

### Basic usage

Point Cargo at the adapters in `~/.cargo/config.toml`:

```toml
[build]
rustc-wrapper = "build-limits-rustc"
rustdoc = "build-limits-rustdoc"
```

Then start the supervisor and build as usual:

```bash
systemctl --user start buildlimits-supervisor.service
build-limits-status   # capacity 2, held 0, healthy, caching through sccache
cargo build
```

A build that has to wait says so on standard error:

```plaintext
[build-limits] serde: waiting for a compile slot (1 ahead, 45s)
```

______________________________________________________________________

## Features

- `build-limits-rustc` and `build-limits-rustdoc` adapters that preserve the
  caller's streams, exit status, umask, niceness and resource limits.
- `build-limits-supervisor`, a systemd user service that places each compiler
  group in its own transient scope and composes it with a managed sccache.
- `build-limits-status` for capacity, held tokens, the queue and drain faults.
- `build-limits-recover` to account for cache work that outlived its build,
  without ever releasing a token on a guess.
- `build-limits-metrics` to summarize bounded, identifier-free journal
  metrics.
- Admission uses the Python standard library; OpenTelemetry SDK supplies traces.

______________________________________________________________________

## Learn more

- [User's guide](docs/users-guide.md) — installation, configuration, status,
  recovery and metrics
- [Developer's guide](docs/developers-guide.md) — quality gates, internals and
  the test suite
- [Repository layout](docs/repository-layout.md) — where everything lives

______________________________________________________________________

## Licence

ISC — see [LICENSE](LICENSE) for details.

______________________________________________________________________

## Contributing

Contributions are welcome! Please read [AGENTS.md](AGENTS.md) and the
[developer's guide](docs/developers-guide.md) before opening a pull request.
