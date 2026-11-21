# buildlimits user's guide

This guide is for operators of a shared development host and for anyone whose
Cargo builds run through compile admission. It explains what buildlimits does,
how to install and configure it, what a build sees while it waits, and how to
read status, recover a retained cache and interpret metrics. Contributor
workflows live in the [developer's guide](developers-guide.md).

## What buildlimits does

buildlimits limits how many Cargo compiler groups run at once across a whole
machine — for every agent, repository and worktree — without changing any build
command. A [guildmaster](https://codeberg.org/amonakov/guildmaster) token pool
at `/dev/guild` holds the machine-wide budget. Each user runs one admission
supervisor, which takes one token per compiler group and returns it only when
every process of that group has exited. Compilations still go through sccache
when it is available.

Cargo reaches the supervisor through two small adapters that it already knows
how to call: one stands in as `RUSTC_WRAPPER`, the other as `RUSTDOC`. Nested
compilations inside an admitted group, such as the ones rustdoc starts, run
directly and never ask for a second token.

Real crate compilations still require a token. `build-limits-rustc` also runs a
small allow-list of toolchain queries without admission: version queries,
selected no-input `--print` queries, Cargo's stdin target-information query,
and those queries behind the recognized Clippy and Dylint workspace drivers.
Unknown wrappers, compile arguments and rustdoc invocations stay on their
normal paths. The
[query admission RFC](rfcs/rfc-0003-toolchain-query-admission-exemption.md)
records the exact grammar and its evidence.

| Command                          | Role                                                     |
| -------------------------------- | -------------------------------------------------------- |
| `build-limits-rustc`             | `RUSTC_WRAPPER`: runs one compiler under admission       |
| `build-limits-rustdoc`           | `RUSTDOC`: runs the toolchain's rustdoc under admission  |
| `build-limits-supervisor`        | The per-user admission supervisor, run as a user service |
| `build-limits-status`            | Prints capacity, held tokens, the queue and groups       |
| `build-limits-recover`           | Runs or inspects a managed-cache recovery transaction    |
| `build-limits-metrics`           | Summarizes bounded compile-admission journal metrics     |
| `build-limits-cache-replay`      | Reads the local outcome history for H2 and H3 evidence   |
| `build-limits-outcome-archive`   | Archives closed outcome history and applies retention    |
| `build-limits-predict-dataset`   | Snapshots recorded outcomes for offline prediction       |
| `build-limits-predict-benchmark` | Compares cache-hit predictors over a snapshot            |

_Table 1: Installed commands._

## Requirements

- Linux with cgroup v2 and a systemd user manager reachable through `busctl`.
  Each admitted compiler group runs in its own transient scope under
  `buildlimits-compilers.slice`.
- A guildmaster token pool at `/dev/guild`, readable and writable by the user's
  `guild` group membership.
- Python 3.14 or newer. Admission uses the standard library; the OpenTelemetry
  SDK is the runtime dependency for tracing.
- Optionally, sccache, run as the managed `buildlimits-sccache.service` user
  unit when the supervisor composes compilers with the cache.

## Installation

The package installs as an ordinary Python distribution with ten console
scripts. The [uv](https://docs.astral.sh/uv/) tool installer keeps it isolated
from project environments:

```bash
uv tool install --python 3.14 git+https://github.com/leynos/buildlimits
```

On managed hosts, the `devhost.buildlimits` Ansible collection in
[dev-env-rocky](https://github.com/leynos/dev-env-rocky) provisions everything
around the package: the guildmaster pool and its group membership, the
`supervisor.toml` file, the `buildlimits.slice`, `buildlimits-supervisor` and
`buildlimits-sccache` user units, and the handover of Cargo's wrapper settings.
The collection's role documentation covers provisioning switches, staging and
the maintenance procedures that need privileged access.

## Quick start

With the pool present and the supervisor's user unit installed, point Cargo at
the adapters in `~/.cargo/config.toml`:

```toml
[build]
rustc-wrapper = "build-limits-rustc"
rustdoc = "build-limits-rustdoc"
```

Start the supervisor and confirm that it is healthy:

```bash
systemctl --user start buildlimits-supervisor.service
build-limits-status
```

```plaintext
capacity 2, held 0, healthy, caching through sccache
```

Any `cargo build`, `cargo test` or `cargo doc` now waits for a slot when the
machine is busy, and runs as soon as one is free.

## Configuration

### Supervisor settings

The supervisor reads `~/.config/buildlimits/supervisor.toml`, or the file named
by `build-limits-supervisor --config PATH`. Every key is optional; a missing
file yields the defaults. Unknown keys and wrongly typed values stop start-up
with exit status 2, and nothing is half-applied.

| Key                                     | Default                                        | Meaning                                                                                                                                                                                                     |
| --------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `device`                                | `/dev/guild`                                   | The guildmaster token device                                                                                                                                                                                |
| `slots`                                 | `2`                                            | The pool's configured capacity, reported by status                                                                                                                                                          |
| `socket_path`                           | `$XDG_RUNTIME_DIR/buildlimits/supervisor.sock` | Where the supervisor listens                                                                                                                                                                                |
| `lock_path`                             | `$XDG_RUNTIME_DIR/buildlimits/supervisor.lock` | The singleton lock                                                                                                                                                                                          |
| `compilers_slice`                       | `buildlimits-compilers.slice`                  | The slice that holds every compiler scope; must end in `.slice`                                                                                                                                             |
| `cancel_grace_seconds`                  | `10.0`                                         | Seconds between `SIGTERM` and `SIGKILL` for a terminated group                                                                                                                                              |
| `drain_timeout_seconds`                 | `120.0`                                        | The longest start-up waits for surviving groups to drain                                                                                                                                                    |
| `cache_mode`                            | `sccache`                                      | `sccache` composes admitted compilers with the managed cache; `kache` composes them with an unowned kache daemon; `direct` runs them without a cache. See [Choose a cache backend](#choose-a-cache-backend) |
| `cache_outcomes`                        | `off`                                          | `record` opts in to observation-only cache outcome recording; it requires `cache_mode = "sccache"`                                                                                                          |
| `cache_outcomes_max_age_days`           | `30`                                           | Oldest retained outcome age in days; `0` disables age eviction                                                                                                                                              |
| `cache_outcomes_max_bytes`              | `12582912` (12 MiB)                            | Logical uncompressed bytes retained across active, sealed and archived history; `0` disables the byte bound and requires segmentation. The default is the legacy 12 MiB bound                               |
| `cache_outcomes_max_records`            | `0`                                            | Retained-record cap; `0` leaves segmented history uncapped and keeps the legacy 10,000-row cap otherwise                                                                                                    |
| `cache_outcomes_archive_headroom_bytes` | `25165824` (24 MiB)                            | Temporary disk space an archive run may use; never counted against retained bytes. Must hold at least one 12 MiB segment                                                                                    |
| `cache_outcomes_archive_preset`         | `6`                                            | XZ preset from 0 (fastest) to 9 (smallest)                                                                                                                                                                  |
| `sccache_executable`                    | `sccache`                                      | The sccache client used in `sccache` mode                                                                                                                                                                   |
| `sccache_unit`                          | `buildlimits-sccache.service`                  | The managed cache's user unit                                                                                                                                                                               |
| `sccache_socket`                        | `$XDG_RUNTIME_DIR/buildlimits/sccache.sock`    | The managed cache's socket                                                                                                                                                                                  |
| `cache_recovery`                        | `starved`                                      | `starved` or `manual`; see [Recover a retained managed cache](#recover-a-retained-managed-cache)                                                                                                            |
| `recovery_marker`                       | `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`  | The restart barrier the managed cache unit honours                                                                                                                                                          |
| `kache_executable`                      | `kache`                                        | The kache client used in `kache` mode; see [Choose a cache backend](#choose-a-cache-backend)                                                                                                                |
| `kache_socket`                          | **required** in `kache` mode                   | The separately managed kache daemon's socket, exported as `KACHE_SOCKET_PATH`; there is no default                                                                                                          |
| `kache_cache_dir`                       | unset                                          | The kache store, exported as `KACHE_CACHE_DIR` and checked by health                                                                                                                                        |

_Table 2: Supervisor configuration keys._

Every `cache_outcomes_*` retention key requires `cache_outcomes = "record"`;
validation rejects one written while recording is off.

Sockets live under `$XDG_RUNTIME_DIR/buildlimits`, falling back to
`/run/user/UID/buildlimits` when the variable is unset, because Unix socket
paths are limited to about 108 bytes.

`sccache_executable` and `kache_executable` may each be an absolute path or a
bare name. A bare `sccache_executable` stays in the composed vector and is
searched on the build's `PATH`; sccache's health probe runs no executable, so
nothing else resolves it. A bare `kache_executable` is resolved once on the
supervisor's own `PATH`, and the file it resolves to is the one both the health
probe and every composed group run. A supervisor started by systemd inherits a
minimal `PATH`, so configure an absolute path for a kache client installed
outside the usual directories.

```toml
slots = 4
cache_mode = "sccache"
cache_recovery = "manual"
cache_outcomes = "off"
```

### Record cache outcomes

Outcome recording is off by default. Set `cache_outcomes = "record"` only after
installing the recording-capable build and all journal readers that understand
its event vocabulary. Recording requires `cache_mode = "sccache"` and a
supported server version. This release supports sccache 0.14.0. It observes
completed cached requests; it never changes admission, cache composition or
compiler execution.

The local store is `$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl`, or
`~/.local/state/buildlimits/cache-outcomes.jsonl` when `XDG_STATE_HOME` is
unset. Its directory is private to the user (mode `0700`); the store and its
sibling `cache-outcomes.salt` use mode `0600`. The salt keys compile identity
digests. Stored values include a crate name and origin class, salted digests of
arguments and target, a bounded toolchain identity, the observed cache result,
and coarse runtime and parser-class fields. Raw compiler arguments, local
paths, environment values, request identifiers, source code and compiler output
are not stored.

The status command adds a `cache outcomes recording` line while recording is
enabled; once a reading or outcome has closed, that line also names the last
reason, which is the first thing to check when nothing is being attributed. If
recording stops after an internal failure, the line reports that and keeps the
last reason when one was recorded. If a single status read cannot build the
outcome snapshot, the line reads `cache outcomes status unavailable`; recording
continues and a later read may recover, so this is not a recorder failure. For
history and reading or store failures, query:

```bash
build-limits-metrics --since 1d --max-records 100000
build-limits-cache-replay h3
build-limits-cache-replay h2
```

The metrics report's `cache_outcomes` section groups observed outcomes and
attribution, reading failures and skipped readings, store operations, child
sightings and sampling failures. H2 and H3 replay read the local store only. H3
compares the parser prediction with attributed outcomes and refusal summaries.
H2 combines full-emit child sightings with prior hit runtimes for the same
compile key. The runtime clue compares each row with the median of the key's
most recent 64 earlier hits, on every replay path, so a key built thousands of
times cannot make replay memory grow with the history. That window is a
deliberate classifier change from earlier releases: a history whose key has
more than 64 earlier hits can produce a different runtime and combined
agreement figure, and so a different H2 verdict, although every reported field
and the report schema are unchanged. H2 evaluates only sampled keys that have
both a hit and a miss, and requires 100 evaluable rows and 2,000 attributed
outcomes. H2 reports `supported` at 95% agreement, `refuted` below 90%, and
`inconclusive` between those thresholds. Both commands print the time span the
store covers and the limits of their sample: labels come from solo and
unanimous windows, so a verdict may not represent busy, mixed traffic, and H2
also states that child sightings are sampled, which biases the sighting clue
towards long misses.

While cached requests are moving, the supervisor samples the managed server's
control group every 250 ms and inspects at most 64 processes per sample. It
matches full-emit children to active requests by `-C metadata` in memory. The
store keeps only a sighting token and sample count; it never stores arguments
or metadata values. If active requests share metadata, the sighting is
`ambiguous` and contributes no child-only H2 evidence. `not_seen` records mean
that at least one sample found no matching child; short compiler processes may
finish between samples.

This is evidence collection, not a hit prediction shown to users or used by the
supervisor.

To reset the history completely, stop the supervisor and delete
`cache-outcomes.jsonl`, `cache-outcomes.jsonl.bad` and `cache-outcomes.salt`,
then start it again. This loses replay history, including any `.bad` backup
kept from a corrupt store, and creates a new salt, so future keys no longer
join to the deleted store. A corrupt or version-mismatched store is moved to
`.bad` at recording startup, but only when that backup does not already exist;
otherwise recording remains unavailable. Read-only replay leaves the store
untouched and reports the decoding failure.

For an upgrade, install every journal reader first, then the supervisor build
while recording remains off. Set `cache_outcomes = "record"` only after that
build is active. To downgrade, set the option to `off` and restart gracefully;
remove the key before downgrading to a build that predates this setting. When a
new sccache version produces `unsupported_version`, add the version and its
fixtures to `SUPPORTED_SCCACHE_VERSIONS` in the recorder as one change and run
`make test-sccache-host` against it. The host check runs for every supported
version instead of skipping, so merge that change only once the check passes.

If the opt-in real-binary check cannot run, follow
[ADR 005's private real-binary fallback procedure](adrs/adr-0005-cache-outcome-recording.md#d9-keep-the-real-binary-check-private-and-non-blocking).
Build the same small crate once cold and once warm through the normal admitted
supervisor, then compare adjacent sccache statistics windows. Do not bypass
admission with a private compile. The recorder writes final outcome labels and
keeps no provisional marker, so a fallback run produces no confirmed H2 or H3
evidence; treat what it observes as unconfirmed.
[ADR 005's counter confirmation rule](adrs/adr-0005-cache-outcome-recording.md#d12-confirm-provisional-labels-only-if-needed)
applies only if a trailing counter is demonstrated, and this release does not
demonstrate one.

### Archive cache-outcome history

The supervisor writes outcomes into the active store; it never compresses
history. A separate timer-facing command, `build-limits-outcome-archive`, moves
closed history into verified XZ archives and applies the configured retention
policy. It runs outside the supervisor, so archiving cannot delay admission or
token accounting.

Run it from a systemd user timer as the same user that runs the supervisor. A
oneshot service and a daily timer are enough:

<!-- tested-example: buildlimits-outcome-archive-service -->
```ini
# ~/.config/systemd/user/buildlimits-outcome-archive.service
[Unit]
Description=Archive closed buildlimits cache-outcome history

[Service]
Type=oneshot
ExecStart=%h/.local/bin/build-limits-outcome-archive run
MemoryMax=512M
TimeoutStartSec=2h
```

<!-- tested-example: buildlimits-outcome-archive-timer -->
```ini
# ~/.config/systemd/user/buildlimits-outcome-archive.timer
[Unit]
Description=Daily buildlimits cache-outcome archive

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer with:

<!-- tested-example: buildlimits-outcome-archive-enable -->
```bash
systemctl --user enable --now buildlimits-outcome-archive.timer
```

`Persistent=true` makes the timer run once at the next start after a missed
firing, so a host that was off still archives its closed history. If
`uv tool install` placed the command elsewhere, use that absolute path: a user
unit does not read your shell's `PATH`. A second run that starts while one is
in progress exits with status 75 rather than waiting or racing.

A user unit also does not read your shell's environment, so the command falls
back to the same defaults the supervisor uses: `--config` resolves to
`~/.config/buildlimits/supervisor.toml`, and `%h` in `ExecStart` expands to
your home directory. If the supervisor reads a different configuration file,
pass `--config` with its absolute path in the unit; the retention keys come
from that file, never from the unit's environment.

The store path has the same caveat. The user manager resolves `XDG_STATE_HOME`
from its own environment, which normally holds the value it was started with
rather than the one in your current shell, so the timer can look in a different
directory from the supervisor. If the supervisor writes under another
`XDG_STATE_HOME`, pass `--store` with the store's absolute path in the unit. A
unit that sets neither archives whatever the default resolves to, and an empty
run is still a success, so check `last-run.json` in the archive directory after
the first firing.

Use `inspect` to see what a run would do without changing a file or a
directory's mode, or `run --dry-run` to take no lock and write no state. Both
still validate the store's directories, so a symlinked or shared component is
refused exactly as a run would refuse it. `run` performs the plan under its own
lock:

<!-- tested-example: buildlimits-outcome-archive-commands -->
```bash
build-limits-outcome-archive inspect
build-limits-outcome-archive run --dry-run
build-limits-outcome-archive run
```

All three accept `--store PATH`, `--config PATH` and `--json`. `--config`
defaults to `~/.config/buildlimits/supervisor.toml`, the file the supervisor
uses, so the command reads the same retention keys. `--store` names the active
store file; without it, the default is the one described under
[Record cache outcomes](#record-cache-outcomes). The plain output is a bounded
set of status lines; `--json` prints the same report as one stable document
with sorted keys. Neither form prints a filesystem path on a successful run.
The one exception is an unreadable configuration file, which is reported on
standard error before any report exists and does name the file.

Every plain report also names when the firing started and how long it took, so
a journal reader can tell a slow run from a stalled one:

```text
started: 2026-11-19T17:30:00Z
elapsed: 1.250s
```

Both figures come from the report's own clock readings, so `elapsed` is the
span the report describes rather than the time the reader happens to run it.
The JSON form carries the same times as `started_at` and `finished_at`.

A reading that has to be retried writes one bounded line per attempt to
standard error, such as `retry: snapshot attempt 2 reason inventory_changed`.
The line names the operation, the attempt and a closed reason, never a path, so
a store the writer keeps changing is visible in the timer's journal.

Each firing also publishes one `archive_run` metric record: its status and
reason, the archived, trimmed, deleted and quarantined action counts, the
number of failed sequences, and the elapsed seconds. Records go to the native
journal only when systemd started the command, because a shell session has no
journal stream to address; they never include a path.

The plain report's `retained:` line gives the logical bytes against the budget,
and its `history:` line gives the rows the store retains, the observed span
they cover and, when any segment's metadata is missing, how many entries could
not be measured:

```text
history: 1240 records, observed 2026-11-01T08:00:00+00:00..2026-11-19T17:30:00+00:00
history: 1240 records, 2 unmeasured, observed 2026-11-01T08:00:00+00:00..2026-11-19T17:30:00+00:00
```

An inspection or dry run reports the bytes the store still holds, not the
estimate after the eviction it plans; the JSON plan carries that post-eviction
estimate as its own `retained_logical_bytes`.

The count covers the closed rows that segment metadata describes and every row
still in the active file, including rows the age rule has already expired and
the writer has not yet compacted. Those rows are not retained history, so the
budgets described below do not count them; the report describes what the store
physically holds until the writer's next compaction. An unmeasured entry, such
as an archive whose sidecar was lost or a file that cannot be decoded,
contributes no rows and no span, so a report never presents an unknown part of
the history as empty. An archive is refused as corrupt when its final row is
torn: publication always terminates an archive, so a fragment at the end means
the file is not the archive a run published, and counting it would overstate
retained history. The JSON carries the same figures as `retained_records`,
`observed_from`, `observed_until` and `unmeasured_entries`.

The writer seals the active file when an append would take it past 12 MiB, so
the archiver only ever rewrites closed segments. The archiver compresses each
sealed segment to XZ, verifies the archive against its source, publishes it,
and only then removes the plain source. A segment that retention has placed
wholly outside the bounds is the exception: it is removed without being
compressed, so a run never spends its scratch space archiving history the same
plan deletes. It holds a private lock of its own and never writes or replaces
the active file. It does read the active file to measure the rows and span the
writer currently holds; the read is bounded and refuses a path that is not a
regular file. The size and the rows come from the same opened version, so a
writer that replaces the path mid-read cannot pair one version's bytes with
another's rows. A writer that appends while the scan runs is detected by
comparing the size immediately before and after it, and the scan is retried so
both figures describe the same version; a final row whose newline has not been
written is the writer's append in flight, which a failed write truncates back
out, so it is not counted.

Retention is one policy shared by the writer, the archiver and the read-only
history view:

- The byte budget counts logical uncompressed bytes across active, sealed and
  archived history. An archive's contribution is its sidecar's `logical_bytes`,
  so compression does not move the boundary. The active file contributes only
  the bytes of the rows the age rule keeps, because a row the writer will
  compact away is not retained history; counting it would evict closed history
  to make room for bytes retention has already released. When the active file
  cannot be decoded, its committed bytes are unknown, so the byte budget is
  deferred rather than decided from the whole file, whether or not an age
  cutoff applies: the whole file includes any append the writer has not
  committed and may roll back. The run reports `unsettled` and exits 75, and
  the next firing retries.
- Eviction is oldest-first. A record whose observation time equals the age
  cutoff is retained; one older than the cutoff is not.
- The record cap bounds the retained record count across active, sealed and
  archived history. The archiver streams the active file once per plan to count
  the rows the age rule keeps, and counts each sealed source's recorded count,
  so a store cannot hold the cap in archives while the writer fills another cap
  beside it. It removes whole oldest archives while they fit inside the excess,
  then trims the archive that straddles the boundary, so a segmented history is
  capped rather than exempt. A sealed source is closed history too and is
  evicted whole when the cap reaches it before the run has compressed it; a
  source that straddles the boundary is archived first, and the next plan trims
  its archive. Only the writer's active file is never trimmed, because the
  writer owns it. A cap of zero leaves segmented history uncapped.

A single firing may apply retention over more than one pass. The byte and
record walks act on the rows that survive the age rule, so while an age trim is
still pending the run plans no whole deletion and no byte or record trim; it
applies the age trim first and re-plans from the measured history. The same
applies after a duplicated source is reconciled, while an archive's sidecar is
missing or corrupt, and while a sealed source has no writer metadata: none of
them is measured well enough to choose a whole deletion or a trim. A walk that
reaches an entry it cannot yet rewrite, such as a sealed source larger than the
remaining allowance, also stops there and leaves the rest of the budget to the
follow-up pass. The writer does not take the archive lock, so it can also seal
a source after the run has planned. Each follow-up pass re-reads the store and
archives what it finds before applying the budget, so a source that arrives
mid-run is still measured and capped in the same firing. A pass that
quarantines a damaged archive or sealed source stops before eviction, because
the rows it removed were part of the total the budget was planned over; the
next firing decides retention from what remains, and the run still reports the
corruption at exit 3. The passes are bounded: if new seals keep arriving and
work remains, the run reports `status: unsettled` and exits 75, and the next
timer firing continues. The final report is read the same way: the inventory is
scanned on both sides of the active-file read and the reading is retried when
they disagree, so a seal cannot leave either copy out of the count. A writer
that seals on every reading leaves the report unsettled too, rather than
recording one partial reading as the run's result. The final report also
re-plans retention from that reading: when the writer appends or seals past a
bound after the last retention pass, the run reports `unsettled` at exit 75
rather than telling the timer the store is within its bounds while the next
firing still has to evict. `inspect` and `run --dry-run` plan and report from
that same settled reading, so the plan and its figures describe one version of
the store; a store that never holds still makes them report `unsettled` with no
plan and exit 75. The report's `active_bytes` comes from the reading its row
count came from.

A plan that leaves a configured budget to a later pass reports
`retention: deferred` on its plain output and `"retention_deferred": true` under
`plan` in JSON. Its `trim` and `delete` counts describe only what the first
plan could decide, so `inspect` and `run --dry-run` cannot be read as the run's
final eviction; the run's `actions` list records what it performed.

The writer also enforces the age bound when it starts, and on every append: an
active file whose oldest record predates the cutoff, and which is not also
oversized, is compacted as the store loads, and a row that arrives already
expired is dropped once it is appended. A host that was idle past the bound, or
one that only receives old observations, therefore does not carry its expired
rows forward indefinitely. An oversized file is sealed first, and the
archiver's next run applies the age policy to that segment.

The retention keys and their defaults are in [Table 2](#supervisor-settings).
The 12 MiB default is the legacy bound, so a host that writes none of the keys
keeps its current store shape. Set `cache_outcomes_max_bytes` to a larger
logical budget to retain more history. Every retention key requires
`cache_outcomes = "record"`.

The 15,000,000,000-byte budget this design targets is not the default. Write
`cache_outcomes_max_bytes = 15000000000` in `supervisor.toml` before enabling
the timer, or the run retains only the legacy 12 MiB. Provisioning that key,
and the timer itself, on a managed host is outside this release; configure them
on the host that runs the archiver.

The writer and archiver keep their state beside the store:

- `cache-outcomes.seq` is the writer's next-sequence counter.
- `cache-outcomes.sealed/` holds closed plain segments,
  `segment-NNNNNNNNNNNN.jsonl`, with `segment-NNNNNNNNNNNN.jsonl.meta.json`
  sidecars.
- `cache-outcomes.archive/` holds `segment-NNNNNNNNNNNN.jsonl.xz` archives
  with `.jsonl.xz.json` sidecars, plus `retention.json`, `last-run.json`,
  `archive.lock` and `quarantine/`.

The archiver refuses a store whose directories, or any directory above them,
are symlinks, are not directories, or are writable by another user; such a run
exits 1 with `reason: unreadable` and prints no path. Keep the store in a real,
private directory tree. A root-owned directory with the sticky bit set, such as
`/tmp`, is accepted as an ancestor, but the store's own directories must be
private and owned by the account that runs the archiver. The active file is
inspected the same way: only an absent file counts as empty history. A path
that exists but is not a regular file, or one that cannot be inspected, ends
the run at exit 1 with `reason: unreadable` instead of being counted as an
empty store. A run with a record cap also exits 1 when the active file's rows
cannot be decoded, because it cannot show that the store is within the cap.

An archive preserves the version-1 record schema and the salt identifier, so
decompressing one yields the segment's JSON Lines content byte for byte apart
from a repaired torn tail. Archived records keep the same privacy boundary as
the active store: they contain the same redacted fields and no raw arguments,
paths or compiler output.

`build-limits-cache-replay` and `build-limits-predict-dataset` can read the
whole retained history. `--history` reads archived, sealed and active history
for the newest salt domain; `--salt-id ID` reads one named domain; and
`--all-salts` reads every domain in append order. Both `--salt-id` and
`--all-salts` imply `--history`. With none of these flags, each command reads
only the active store, exactly the rows an existing invocation read before. A
history read returns the same committed rows as that active-store read: a final
row whose newline has not been written is the writer's append in flight, and it
is left out however the history is read. That is a statement about the input,
not about the H2 classifier: the runtime window described above applies to
every replay path.

A report read across domains names every salt it counted, so a figure that
spans a salt change is never attributed to the newest domain alone. The list is
bounded: at most three identifiers are printed before it says that more exist,
so a damaged store cannot make the provenance line unbounded.

To reset history completely, stop the supervisor before removing the store.
Deleting only `cache-outcomes.jsonl` no longer clears retained history once
segments exist: also remove `cache-outcomes.seq`, `cache-outcomes.sealed/` and
`cache-outcomes.archive/`, together with the salt and `.bad` files described
above.

The command maps every outcome to a closed exit status a timer can act on:

- `0`: success.
- `1`: an operational failure, such as an unreadable store or archive
  directory.
- `2`: usage or invalid configuration. This is the only case that can print a
  local path, on standard error.
- `3`: corruption that needs an operator, such as a segment that cannot be
  decoded, a damaged archive, or a sidecar that cannot be repaired.
- `75`: a temporary condition, such as another run holding the lock,
  insufficient temporary headroom, or history whose retention or report could
  not be settled from a stable reading because the writer kept changing the
  store. The next timer firing should retry.

### Size the archive timer unit

`make outcome-archive-bench` measures one segment and prints its sizes, the
elapsed time, the peak resident memory and the scratch disk the pass used. Set
`SEGMENT=/path/to/segment.jsonl` to measure a real segment instead of a
generated one. At the default preset, on the development host:

| Source                   | Records | Archive | Elapsed | Peak RSS | Scratch |
| ------------------------ | ------- | ------- | ------- | -------- | ------- |
| 6.4 MiB recorded history | 10,296  | 538 KB  | 0.9 s   | 124 MB   | 538 KB  |
| 12 MiB                   | 62,830  | 746 KB  | 3.5 s   | 140 MB   | 746 KB  |
| 100 MiB                  | 523,588 | 6.2 MB  | 33 s    | 143 MB   | 6.2 MB  |

The first row measures a store this host actually recorded, not a generated
one: 10,296 rows at 655 bytes each, which is within one per cent of the 661
bytes per record that issue #63 measured on Rohga, so it is the closest
available stand-in for that host's history. Its archive is 8.0% of the source,
against 6% for the generated segments, so a generated input understates the
archive a real backlog leaves behind; the elapsed figures are not comparable
across the two, because the recorded row is a sixth of the smallest generated
segment and the fixed cost of a pass weighs more on it.

The peak does not follow the segment: eight times the rows moved the peak
resident set by 3 MB. What dominates it is the compressor's dictionary, which
the report also gives as a traced peak of about 98 MB; that figure tracks the
XZ preset, not the history. Compare `peak_rss_bytes` with a unit's limit, and
leave headroom for the interpreter: `MemoryMax=512M` is ample for preset 6. Run
the benchmark as a plain command to get the traced peak. A process that is
already tracing, such as one started with `PYTHONTRACEMALLOC=1`, keeps its own
peak and the report states `peak_python_bytes` as zero rather than discarding
or misattributing that peak.

Throughput is about 3 MB/s of logical input, so a first run over a 15 GB
backlog takes roughly 80 minutes, while a daily run over one day's segments
takes seconds. Set `TimeoutStartSec` to cover the backlog rather than the
steady state, for example `TimeoutStartSec=2h`. Scratch use is bounded by the
archive the pass writes. The `cache_outcomes_archive_headroom_bytes` setting
requires that much free space when a run starts; it is a check, not a
reservation, so another writer that fills the filesystem after the check still
stops the pass. A run that meets a full filesystem reports the retryable
status: nothing is corrupted, and the next timer firing picks the work up once
space returns.

### Compare offline hit predictors

Two targets compare candidate hit predictors against the outcomes this host has
already recorded. They are offline: neither changes admission, cache
composition nor compiler execution.

| Target                   | Writes                                             |
| ------------------------ | -------------------------------------------------- |
| `make predict-dataset`   | `build/predict/history.jsonl`: a redacted snapshot |
| `make predict-benchmark` | `results.json`, `results.md` and `timings.json`    |

`make predict-benchmark` creates the snapshot when it is missing, but only
refreshes an existing one through `make predict-dataset`. The snapshot holds
hashed compile identities and the observed result of each request; it never
holds crate names, arguments or paths. The benchmark replays the snapshot at
each event's admission time, so a predictor is scored only for events whose
admission time was recorded no later than their own outcome; a prediction never
reads a label observed after the moment it was made. Its tables are written to
`build/predict`.

The commands behind those targets take explicit paths:

<!-- tested-example: buildlimits-predict-commands -->
```bash
build-limits-predict-dataset --output build/predict/history.jsonl
build-limits-predict-benchmark --snapshot build/predict/history.jsonl \
  --output build/predict
```

`build-limits-predict-dataset` reads this host's store unless `--store` names
another one, and `--journal-span` or `--skip-journal` control the journal note
recorded with the snapshot. Its `--output` may not name any file it reads: the
active store, a sealed source, a published archive, and the archive a sealed
source will become. That last path need not exist yet. The archiver may publish
it and remove the sealed source while the history is being read, and a snapshot
written there would replace the archive with a document the store cannot read
back. An output inside the sealed or archive directory is refused as well, even
when no inventory has listed its name yet, because the archiver can create a
segment there while the history is read. Such an output is refused with status
2 before any history is read. `build-limits-predict-benchmark` accepts
`--snapshot`, `--output` and `--config`; without `--config` it uses the pinned
parameters in `buildlimits/predict/config.json`. No file the benchmark writes
may name the snapshot, so a destination that reaches it under another spelling
— a relative path, a symbolic link or a hard link — is refused before the run
starts: writing over the history it was read from would make the run impossible
to repeat. The report directory may otherwise hold the snapshot, as the example
above does.

A snapshot with fewer than 2,000 labelled events produces the status
`insufficient data`, with the counts that were seen. A snapshot that holds no
usable admission time produces `insufficient admission times` instead: its
records cannot show what was knowable before each decision, so no prediction is
reported. A snapshot that carries admission times is also refused when its
recorded observation times run backwards, because the store stamps each record
when it appends it: a later position with an earlier stamp means the clock
stepped back, and the recorded times can no longer order real moments. The
history is then reported as unreadable rather than scored. The report never
pads the history with synthetic rows to reach a threshold, and a negative
comparison is printed as found rather than explained away. Both commands exit
with status 0 on success, 1 when the snapshot or store cannot be read or the
output cannot be written, and 2 on invalid usage or an unreadable configuration.

### Adapter environment

The adapters read only their environment, so the same settings work for Cargo,
agents and shells alike.

| Variable                        | Default                         | Effect                                                  |
| ------------------------------- | ------------------------------- | ------------------------------------------------------- |
| `BUILD_LIMITS_SOCKET`           | the supervisor's default socket | Overrides the socket the adapters connect to            |
| `BUILD_LIMITS_UNAVAILABLE_SECS` | `300`                           | How long an adapter waits for an unreachable supervisor |
| `BUILD_LIMITS_HEARTBEAT_SECS`   | `RUSTC_HEARTBEAT_SECS`, or `45` | Interval between waiting and compiling progress lines   |
| `BUILD_LIMITS_RUSTUP`           | `rustup` on `PATH`              | The rustup used to resolve the real rustdoc             |
| `BUILD_LIMITS_COMPILERS_SLICE`  | `buildlimits-compilers.slice`   | The slice whose scopes count as already admitted        |

_Table 3: Adapter environment variables._

The two duration variables take seconds. A value that does not parse, or is not
a finite positive number, is ignored as if it were unset, so a mistyped setting
falls back to the default instead of failing the build.

## Choose a cache backend

`cache_mode` names the cache that admitted compilers are composed with. The
default, `sccache`, points them at the managed server this supervisor starts,
stops and recovers. `kache` points them at a daemon somebody else already runs.
`direct` composes no cache at all.

| Mode      | Composes with      | Work outlives the group | Recovery |
| --------- | ------------------ | ----------------------- | -------- |
| `sccache` | the managed server | yes                     | yes      |
| `kache`   | an unowned daemon  | no                      | inert    |
| `direct`  | nothing            | not applicable          | inert    |

_Table 4: Cache modes._

The middle column is why the other two follow. sccache runs a cache miss as a
child of its server, outside the compiler's own scope, so the scope can empty
while the compilation still runs; a group's token is held until the server is
verified empty. kache runs the compiler as a child of its wrapper, inside the
group, so an empty scope is the proof itself and the token comes back at once.
A kache group therefore never waits for a server and every detached-only event
-- the start-up stop, the drain release, the recovery transaction -- is absent
in that mode.

### Requirements for kache mode

- kache `1.2.2` or `2.0.0-rc.1`. Other versions are unsupported until the
  verification recorded in
  [ADR 006](adrs/adr-0006-kache-cache-backend.md#on-host-verification) is
  repeated for them. To adopt a release, add it to `tests/kache_host.py`'s
  `SUPPORTED_KACHE_VERSIONS` and run `make test-kache-host` against that
  binary; the check runs for every version in the set rather than skipping, so
  merge the change only once it passes.
- `kache_socket`, naming a daemon that is already listening. The key is
  **required** in kache mode: the supervisor's environment and the requesting
  build's can resolve different kache defaults, so an unnamed socket would let
  the health check accept one daemon while the group talked to another and
  started a second one inside the admitted scope. **The supervisor never
  starts, stops or owns the daemon.** The operator installs and runs it as a
  separate unit; the repository does not ship one. That requirement is the
  operator's to keep: the health check samples the daemon before a group is
  composed, so it narrows the window in which a group is pointed at a stopped
  daemon but does not close it. A daemon that stops between the check and the
  launch leaves the compile to kache's own auto-start on kache 2.0.0-rc.1,
  which creates the daemon inside the group's scope; the stray-process guard
  reports that state afterwards, and no setting of that version prevents it. On
  kache 1.2.2 the supervisor's fixed `KACHE_LOCAL_ONLY=1` setting makes the
  remote-gated daemon start paths inert, so that version does not create a
  daemon here; ADR 006 records the measurement for both versions.
- A `kache_executable` the supervisor can run. A bare name is resolved on the
  supervisor's `PATH`, so configure an absolute path when the supervisor runs
  under a minimal service environment.
- The store kache writes. Name it in `kache_cache_dir` when it is not kache's
  own default of `$XDG_CACHE_HOME/kache` or `~/.cache/kache`: health checks
  only a path the supervisor was told, and a group is only ever pointed at the
  store health checked.

`KACHE_SCHEDULER=0` and `KACHE_LOCAL_ONLY=1` are set in every admitted kache
group. The first stops kache's own scheduler from admitting a compile a second
time under memory pressure, which the pool would never see; the second keeps
its remote backends out of scope. Both are fixed settings of the supervisor,
not tuning knobs.

Writing one backend's settings while another is selected is a configuration
error, not a silently ignored line. With `cache_mode = "kache"`, a document
that also sets `sccache_unit` fails to load with
`setting 'sccache_unit' is only valid when cache_mode is 'sccache', not 'kache'`;
the reverse applies to `kache_socket`, `kache_executable` and
`kache_cache_dir` in `sccache` mode. Switching backends therefore means
removing the other backend's keys, so a mode that was not the one intended
cannot load and quietly do nothing. `direct` names no backend, so every
backend's keys are merely inert there.

Do not set `[cache] ignore_env = true` in a host or project kache configuration
on a host that composes kache. That setting suppresses `KACHE_SCHEDULER` and
`KACHE_LOCAL_ONLY`, so kache's scheduler can admit a compile the pool does not
account for. The supervisor cannot detect this setting; the configuration is
the only place it can be prevented.

If health fails -- the client does not resolve, the store cannot be written, or
no daemon answers on the configured socket -- the build compiles directly with
a notice on standard error and `direct_fallback` as its `cache_path`. Nothing
is held up waiting for a daemon to appear.

A `kache` or `kache-engine` process found outside every admitted scope is
listed in `stray_kache_processes` in the status document, always present so
that an empty list means "none found" rather than "not guarded". When the
reading changes, the guard also writes a plain line to the journal, beginning
`stray kache process outside admitted scopes:`. The guard reports; it never
kills. In kache mode no such process ever holds a token, because no second
admission layer exists to account for.

### What recovery means in each mode

In `sccache` mode, recovery stops the managed server, waits until no live work
can start, releases the tokens that were waiting on it, and restarts it. See
[Recover a retained managed cache](#recover-a-retained-managed-cache).

In `kache` mode there is no service for this supervisor to stop or start, so
`build-limits-recover` answers `no-cache-recovery`. That is the same answer
`direct` gives in [Status](#status): the mode that owns no cache cannot recover
one. `cache_recovery` is accepted in the configuration and does nothing,
because its `starved` default would otherwise make every kache host's
configuration invalid.

## What a build sees

A build that waits for a slot prints one line to standard error, repeated at
the heartbeat interval:

```plaintext
[build-limits] serde: waiting for a compile slot (1 ahead, 45s)
```

A long compilation prints `[rustc-heartbeat]` lines instead. The compiler
itself writes to the caller's own standard streams, received as passed file
descriptors, so Cargo's JSON diagnostics reach Cargo unchanged. When the
managed sccache server is down, a build still runs, uncached, and says so on
standard error. The adapter exits with the compiler's exit code, or ends itself
with the same signal; a signal to the adapter closes its connection, and the
supervisor then terminates that compiler group.

An allow-listed query can finish while every token is held. That exception
keeps a cheap toolchain probe from waiting for admission while Cargo may hold
its host-wide package-cache lock. Outside an already admitted compiler scope,
actual compilations and every uncertain invocation still request a token. Do
not infer that an unfamiliar wrapper or query is safe; the classifier fails
closed.

Run each worktree's gates sequentially within that worktree. Independent
repositories and worktrees may run their gates concurrently: start each gate
once and let real compiles use the admission queue. A healthy pool with every
slot held is normal operation. Check progress with `build-limits-status` rather
than waiting for the whole host to go idle before submitting work.

### Descriptor pressure

When the supervisor is short of file descriptors before it has recorded a
request, it answers `busy`. The adapter retries that refusal with bounded,
jittered backoff under the same unavailability deadline and prints one waiting
line. Once a request is queued, it keeps reporting progress and stays
cancellable. A persistent refusal is an infrastructure error before admission;
the adapter never bypasses admission to execute that compile request directly.

A request whose complete, valid declaration needs more descriptors than the
known total budget receives a final `descriptors` refusal naming the required
count and the budget. Temporary lack of headroom, an unknown budget, or a
truncated declaration that would still fit remain retryable.

If the supervisor cannot accept a connection because of descriptor pressure, it
pauses only its listener and retries on a later tick. The listening socket and
its kernel backlog stay in place, so new arrivals wait while existing requests
and compiler groups continue. This backpressure does not mark admission
unhealthy.

### A real compiler that cannot find libLLVM

A toolchain that ships `rustc-dev` keeps a second `librustc_driver` in
`lib/rustlib/<triple>/lib`. Cargo puts that directory on `LD_LIBRARY_PATH` for
build scripts. If the real `rustc` is run with that value, the loader binds the
duplicate driver, whose `$ORIGIN/../lib` has no `libLLVM`, and the compiler
exits with status 127 before it prints anything. Kani 0.67.0 drives such a
toolchain, `nightly-2025-11-21`.
[ADR 002](adrs/adr-0002-toolchain-library-path.md) records the cause and the
decision.

**Recognize the symptom.** A build script such as `rustversion`'s reads only
standard output, so the failure appears as an empty version rather than as a
missing library:

```plaintext
Error: unexpected output from `rustc --version`: ""

Please file an issue in https://github.com/dtolnay/rustversion
```

**Probe the toolchain Kani drives**, not the repository's own pin, which has no
duplicate and always passes. The probe loads the compiler and compiles nothing,
so it does not need a compile slot:

```bash
KT=$HOME/.rustup/toolchains/nightly-2025-11-21-x86_64-unknown-linux-gnu
RL=$KT/lib/rustlib/x86_64-unknown-linux-gnu/lib
ls "$KT"/lib/librustc_driver-*.so "$RL"/librustc_driver-*.so   # two copies?
ls "$RL"/libLLVM* 2>/dev/null || echo "no libLLVM beside the duplicate"
LD_LIBRARY_PATH=$RL "$KT/bin/rustc" --version; echo "rc=$?"          # 127 if affected
LD_LIBRARY_PATH=$RL:$KT/lib "$KT/bin/rustc" --version; echo "rc=$?"  # 0
```

A duplicate alone does not prove the failure: several `rustc-dev` toolchains
also keep a `libLLVM` in the rustlib directory and start normally. Only a
`rc=127` from the first probe line does.

**What the adapters do.** When `build-limits-rustc` or `build-limits-rustdoc`
runs a real toolchain `rustc` or `rustdoc` (`<root>/bin/rustc` with a
`librustc_driver-*.so` in `<root>/lib`), it prefixes `<root>/lib` to a non-empty
`LD_LIBRARY_PATH` that does not already list it, as the rustup proxy does.
This applies to a compiler admitted by the supervisor and to one the adapter
runs directly inside an admitted scope. Arguments are never changed, and the
proxy, `clippy-driver` and unrecognized compilers see the environment
unchanged. The Clippy and Dylint query exemption affects admission only; it
does not extend loader-path repair to either driver. No operator action is
needed. Processes that were already running keep their old environment.

**What they cannot do.** A build script that Cargo runs, and anything else that
starts a real `rustc` without these adapters, still inherits the poisoned path.
For a Kani-gated suite, put the Kani toolchain's `lib` directory on the path
before running it:

```bash
KT=$HOME/.rustup/toolchains/nightly-2025-11-21-x86_64-unknown-linux-gnu
export LD_LIBRARY_PATH="$KT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
make test
```

The entry works because Cargo appends the inherited value to the rustlib
directory, and the loader finds `libLLVM` through any listed directory. The
workaround belongs to one toolchain, so repeat the probe and change `KT` when a
Kani upgrade changes the pinned toolchain. Test guards that look only for
`error while loading shared libraries`, such as the one in `theoremc`, do not
match this failure.

## Status

```bash
build-limits-status
build-limits-status --json
build-limits-status --ready
build-limits-status --ready --json
journalctl --user --unit buildlimits-supervisor.service
```

`build-limits-status` shows the capacity, held tokens, waiting requests and
running compiler groups. Guildmaster reports none of these, so the status comes
from the supervisor. The plain-text output also explains an unhealthy state,
the current drain fault and any required recovery action. The `--json` document
adds descriptor limits, the open-descriptor sample, request-owned retention,
the high-water mark and the budget. These are point-in-time diagnostics, not
history; `build-limits-metrics` summarizes history.

When `cache_outcomes = "record"`, status also shows the number of attributed
outcomes and completed readings. That optional object and line are absent when
recording is off.

| Exit status | Default: health check                         | With `--ready`                                  |
| ----------- | --------------------------------------------- | ----------------------------------------------- |
| `0`         | Query succeeded and the supervisor is healthy | Healthy and `admission.ready` is exactly `true` |
| `1`         | Query or health failure                       | Query or non-truthy health failure              |
| `3`         | Not used                                      | Readiness is false, missing or malformed        |

_Table 5: `build-limits-status` exit statuses._

Success without `--ready`, or `healthy` alone, is not evidence of usable
admission readiness. The status fields describe separate signals:

- `healthy` reports supervisor and token-pool health. It does not say whether
  admission can make progress.
- `caching` reports whether the managed cache was reachable at its last probe.
  Cache reachability does not determine readiness; direct compilation can be
  admitted while sccache is unavailable.
- `restarting` says the supervisor is winding down for a graceful stop. It can
  remain healthy while running groups finish.
- `drain_fault` gives the latest failed drain-accounting check; its retained
  tokens stay held until accounting succeeds. `drain_fault_kind` classifies the
  failure as `live_work`, `stopped_populated` or `manager`.
- `drain_attempts` counts failed drain checks and resets when the drain fault
  clears.
- `recovery_action` gives operator guidance once the retry threshold is reached.
  `recovery_action_is_fallback` is true when that command is only a fallback
  because automatic recovery is pending. This guidance can be delayed and can
  change independently of admission classification, which uses the immediate
  fault and current recovery outlook.
- `admission.state` is the readiness classification shown below.
  `admission.ready` is true only for `ready`, `saturated` or `draining`.

Each status document is a point-in-time snapshot. Readiness may change after a
query and does not prove that a monitoring consumer observed or acted on it.

Plain status prepends `admission <state>: <reason>` when the state is `blocked`,
`draining`, `closing` or `unhealthy`; `ready` and `saturated` add no line.
Scripts that inspect the first line must account for this prefix. For
provisioning and monitoring, use `--ready` or the JSON readiness field as shown
below.

`--ready` uses the same plain-text or JSON output with the exit-status contract
in Table 5. Use it for provisioning checks and monitoring that need usable
admission:

```bash
build-limits-status --ready
build-limits-status --json | jq -e '.admission.ready == true'
```

For an admission-specific readiness check, inspect `admission` in the JSON
document. Its `state` is one of the values below, `ready` says whether
admission has a current progress path, and `reason` gives a short count-only
explanation without request or group identifiers. `running`, `draining` and
`server_drain` are compiler-group counts from the ledger; `queued` is the
current admission-queue length, even before requester metadata is available.

| State       | `ready` | Meaning                                                                 |
| ----------- | ------- | ----------------------------------------------------------------------- |
| `ready`     | `true`  | The ledger reports capacity available for a compiler group.             |
| `saturated` | `true`  | All slots are occupied; admission can continue when a slot is released. |
| `draining`  | `true`  | Every slot is retained in server-drain, with a current progress path.   |
| `blocked`   | `false` | Every slot is retained in server-drain and operator recovery is needed. |
| `closing`   | `false` | The supervisor is winding down.                                         |
| `unhealthy` | `false` | The supervisor or its token pool is unhealthy.                          |

`draining` reports a present progress path, not a guarantee of eventual
progress. The readiness result is independent of cache reachability: sccache
may be unavailable while direct compilation can still be admitted. A `true`
result does not promise that a queued compile can start immediately; a
`saturated` pool still has to wait for current compiler work to release a slot.
Partial server-drain retention remains `ready` when capacity is available or
`saturated` when all slots are occupied; it is not `draining`. Readiness
failure is an operational signal; it never authorizes releasing tokens or
stopping sccache. Retained tokens remain held until verified stopped-and-empty
accounting permits their release.

Under `descriptors`, `open` is the sampled count of open descriptors, or `null`
with a numeric `sampling_errno` when enumeration failed; the text summary says
`unavailable`. Limits of `-1` mean unlimited. These gauges help track headroom;
they do not prove that descriptors are leak-free.

### Start-up refusal

Before it opens the pool, the supervisor requires the managed sccache service
to be stopped and its control group empty, because a live server with no
visible compiler children may still hold accepted work waiting for a Cargo
jobserver token. A live service does not stop start-up: the supervisor has by
then terminated every compiler scope, so no admitted group can still use the
cache, and it stops the service itself under the restart barrier (see
[Restart the supervisor after an upgrade](#restart-the-supervisor-after-an-upgrade)).

Start-up still exits with status 2 before the status socket opens when it
cannot prove the service stopped and empty: the barrier cannot be written, the
manager refuses the stop, the service does not stop within 45 seconds, it is
stopped but its control group still holds processes, or the manager cannot be
queried. `RestartPreventExitStatus=2` in the unit keeps the refusal sticky.
`build-limits-status` cannot connect yet; read the unit instead:

```bash
systemctl --user status buildlimits-supervisor.service
journalctl --user --unit buildlimits-supervisor.service
```

Restore manager connectivity if needed, wait for the cache's control group to
empty or stop the surviving processes, leave sccache stopped, then start the
supervisor explicitly. After a refused stop the restart barrier file is left in
place, at `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`, so the cache cannot
return unaccounted; the next accounted start-up retires it.

### Unhealthy supervisor and drain faults

An internal error or pool failure makes the supervisor unhealthy: it refuses
new work and drains, retaining tokens for any work it cannot yet account for.
If a drain query fails repeatedly, the supervisor reports an operator action
after eight consecutive failures and keeps retrying every 30 seconds. Its unit
uses `TimeoutStopSec=infinity` so that systemd cannot kill the token owner
before work is accounted. Once the drain is accounted, the unhealthy supervisor
exits with status 1 and `Restart=always` may start a fresh instance through the
same start-up checks.

Cancellation or signal termination of a cached build can also leave a token in
the `SERVER_DRAIN` state while the supervisor stays healthy. That slot stays
held until the cache service is verified stopped and empty; enough retained
slots can pause new admission. The recovery transaction below is the supported
way out.

## Restart the supervisor after an upgrade

After installing new supervisor code, one command starts it without failing a
queued build, killing a running compiler group, or leaving the host needing
operator action:

```bash
uv tool install --python 3.14 --reinstall git+https://github.com/leynos/buildlimits
systemctl --user restart buildlimits-supervisor.service
```

There is no need to wait for an idle reading first. The supervisor unit must
carry `KillMode=mixed`, described under
[Units the restart relies on](#units-the-restart-relies-on). Without the
directive, a supervisor older than the 2026-10-19 amendment to ADR 001 loses a
build when a restart lands while that build is being placed in its scope. A
newer supervisor refuses such a build as `restarting`, and its adapter retries
it. `KillMode=mixed` is still required because it also keeps the stop away from
the supervisor's other helper commands. The restart works as follows.

1. `systemctl` sends `SIGTERM`. The supervisor stops launching and requesting
   tokens but terminates nothing: every running compiler group runs to its own
   end, and its token returns to the pool when its scope is empty.
2. Builds that are waiting, and builds that arrive meanwhile, stay queued. They
   see only a longer wait.
3. When the last group has finished, the supervisor tells every waiting build
   to try again and exits 0. Each adapter reconnects, up to the five minutes it
   allows for an unreachable supervisor, and is queued by the new code. A build
   already admitted is never retried; it finishes first.
4. `systemd` starts the new code. It terminates any surviving scope, stops the
   managed cache if it is still running, proves it stopped and empty, and only
   then opens the pool and reports `READY=1`. The cache is started again behind
   that notification, so it is back within seconds.

`systemctl --user restart` does not return until the new supervisor is ready,
which can be as long as the longest running compile. While it waits, from
another terminal:

```plaintext
$ systemctl --user status buildlimits-supervisor.service
● buildlimits-supervisor.service - Compile admission supervisor
     Active: deactivating (stop-sigterm) since Sun 2026-10-18 21:33:18 CEST; 2s ago
   Main PID: 3338429 (build-limits-su)
     Status: "Stopping: waiting for running compiler groups to finish"
$ build-limits-status
capacity 4, held 2, healthy, restarting once running groups finish, caching through sccache
no requests waiting
g1 running (cached): cargo check in /home/owner/project, 2.3s
g2 running (cached): cargo check in /home/owner/other, 41.0s
```

Waiting requests appear under `waiting` as usual. When the restart is done,
`systemctl --user restart` exits 0 and the unit reads
`Active: active (running)` with the new `Main PID` and
`Status: "Compiler admission ready; managed cache may start"`. In the journal,
the old instance logs `stopping: finishing running compiler groups before exit`
and the new one logs
`start-up found a live managed cache … stopping it under the restart barrier`
followed by `admitting up to N compiler group(s)`. Until the next cache poll, a
few seconds, groups the new supervisor admits compile without the cache, as
after any cache restart.

If the groups that remain are cached ones that ended by signal, their tokens
return only once the cache is verified stopped and empty, so the stopping
supervisor opens the recovery transaction described below by itself, and the
restart continues when it completes. Only the graceful stop does this; the
immediate stop below refuses the queue and cancels the groups instead.

`systemctl --user stop` performs the same graceful stop, except that nothing
starts the new code: waiting builds keep retrying for the five minutes their
adapters allow for an unreachable supervisor, and then fail.

If waiting is not acceptable, cancel the running groups instead. `SIGINT` is
the immediate stop: the queue is refused as `unhealthy`, every running group is
terminated, and `Restart=always` brings the supervisor back through the restart
barrier.

```bash
systemctl --user kill --signal=SIGINT buildlimits-supervisor.service
```

Two limits apply. The first restart onto a release that contains this behaviour
is performed by the previous release's `SIGTERM` handler, which still cancels
running groups; make it at a quiet point. And a user-manager shutdown also sends
`SIGTERM`: the manager's own timeout ends the wait, guildmaster returns the
pool, and the next start-up terminates any surviving scope.

### Units the restart relies on

The restart relies on these directives in `buildlimits-supervisor.service`. All
but `KillMode=mixed` are the ones the role has always shipped; a role that
changes any of them changes the restart's safety.

```ini
[Service]
KillMode=mixed
Type=notify
NotifyAccess=main
Restart=always
RestartPreventExitStatus=2
TimeoutStopSec=infinity
```

`KillMode=mixed` makes a stop send `SIGTERM` to the supervisor's main process
only. The default, `KillMode=control-group`, sends it to every process in the
unit's control group, including the `busctl` that the supervisor is running at
that moment to place a newly admitted compiler in its transient scope. A
restart that landed in that window killed the `busctl`, and the build was lost
with the journal line `cannot create buildlimits-<pid>-<group>.scope:` and
nothing after the colon. With `mixed`, systemd sends `SIGKILL` to whatever is
left in the control group only after the supervisor has exited, so a stray
process can no longer hold a stop open under `TimeoutStopSec=infinity`. Running
compilers are unaffected either way: they live in their own scopes under the
compilers slice, outside the supervisor's control group.
`buildlimits-sccache.service` keeps the default kill mode, because its stop is
the recovery transaction's stop and must leave its control group empty.

A host provisioned before this directive existed still runs the default. To
check, and after the role has rewritten the unit and
`systemctl --user daemon-reload` has run, read the loaded value:

```bash
systemctl --user show buildlimits-supervisor.service -p KillMode
```

```plaintext
KillMode=mixed
```

Until it reads `mixed`, a restart that lands during a placement is still
survivable, because the supervisor refuses such a request as `restarting` and
its adapter retries it, but a stop also signals the supervisor's other helper
commands (cache probes and stop jobs), which the supervisor then has to retry.
The immediate stop is unaffected by the kill mode: `systemctl kill` signals the
whole control group unless `--kill-whom=main` is given.

`Type=notify` makes `systemctl restart` return when the new supervisor is ready.
`TimeoutStopSec=infinity` stops systemd from killing a supervisor that is
waiting for running groups. `Restart=always` with `RestartPreventExitStatus=2`
brings the supervisor back after a health failure or `SIGINT`, but leaves a
start-up that needs an operator down. The unit must carry no `ExecReload`, and
`buildlimits-sccache.service` must not be bound to the supervisor with
`PartOf=`, `BindsTo=` or `Requires=`, so that restarting the supervisor never
stops or restarts the cache by itself. The cache's restart barrier stays:

```ini
[Unit]
ConditionPathExists=!%t/buildlimits/cache-recovery
```

## Recover a retained managed cache

An interrupted cached build can leave accepted work inside the managed sccache
server after its compiler scope has emptied. No compiler process is visible,
yet the work is live, so admission keeps the token, the queue grows and held
tokens never fall. `build-limits-status` reports the condition:

```plaintext
capacity 4, held 2, healthy, caching through sccache
drain fault kind: live_work
drain check failed; tokens retained: retained detached work: live sccache
service may retain detached requests; visible compiler roots=0; operator
accounting required
fallback recovery action: Automatic recovery of the managed sccache service is
pending and needs no operator action yet. As a fallback only, if it does not
start or complete, run build-limits-recover to stop the service under the
admission barrier, verify it is stopped and empty, and release the retained
tokens; tokens remain held until that verification succeeds.
cache recovery idle: automatic recovery may start in 212.0s
waiting 9.0s: cargo check in /home/owner/project
g1 server-drain (cached): cargo check in /home/owner/project, 88.0s
g2 running (cached): cargo build in /home/owner/other, 30.0s
```

Here a cached compile is still running, which defeats the prompt proof, so only
the retained drain's age can open the recovery.

Each prefix is one rendered line; the fault and guidance lines are wrapped here
to fit the page.

Elapsed time, idle CPU and a missing compiler process are never proof of
completion. Only the supervisor's own accounting releases a token, and it
demands a verified stopped-and-empty service. The recovery transaction performs
that accounting in four phases, at most one per supervisor tick:

1. `quiescing` writes the restart barrier,
   `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`, which the cache unit's
   `ConditionPathExists=!` honours, so the cache cannot return early.
2. `stopping` asks the user manager to stop the cache.
3. `accounting` waits until the drain probe verifies the stopped, empty
   service; that verification returns the tokens.
4. `restarting` removes the barrier, then starts the cache again.

With the default `cache_recovery = "starved"` policy, the supervisor opens a
transaction by itself when a retained drain holds a token while requests wait,
on either of two kinds of evidence:

- The cache holds no visible compiler roots and no cached compile is running,
  so the retained work is provably detached. The supervisor acts at once.
- The drain has been retained without a break for 300 seconds. On a busy host
  every freed slot is refilled with a cached compile at once, so the first
  proof may never be available; without this one, a single retained token would
  cost the host a slot until every slot was retained.

Either way the transaction holds new compiles off the cache while it runs, and
it stops the cache only once no cached compile is running. An automatic stop
also wants the cache to show no visible compiler roots, but it will not wait
for them for ever. Once no cached compile is running, a root still visible in
the cache serves no admitted request, because every request admitted since the
barrier was written compiles without the cache. If such roots are seen without
a break for 600 seconds, for example because a compile inside the cache has
hung, the automatic stop proceeds anyway, writes a journal line beginning
`cache recovery: stopping the managed cache with compiler roots` and adds one
to the `recovery_roots_deadline_stops` count of the `cache_recovery` metrics
event. The 600 seconds restart from nothing whenever a cached compile starts
moving again or a probe cannot count the roots, and each transaction measures
its own. A count the probe could not read is never treated as a visible root.
While the roots delay the stop, `build-limits-status` shows
`cache recovery: visible compiler roots have delayed the stop for ...`. A manual
`build-limits-recover` never waits for roots, and a faulted user manager still
refuses every stop.

After each transaction the automatic trigger rests before it may act again: 60
seconds after a clean recovery, and 900 seconds after one whose restart was
still refused, so that a cache slow to come back cannot exhaust a second set of
attempts. While a retained drain waits behind that rest, or behind the stale
proof, `build-limits-status` says so:

```plaintext
cache recovery idle: automatic recovery may start in 42.5s
```

Tokens retained beside that line are waiting for the trigger, not stuck. The
line counts down to the instant the trigger can open: while a drain is retained
with requests waiting, it shows the later of the cooldown and the time left to
the stale proof (300 seconds less the drain's age). It appears only while
waiting will open the trigger. A drain that clears, a queue that empties, a
`manual` policy, a faulted user manager or a non-`sccache` cache mode removes
it, and so does a `starved` policy with nothing retained, even during the
cooldown; the `cooling_for` field still reports the rest. A transaction that
ended with its restart refused also keeps its `cache recovery last fault:`
line, during the rest and after it, until the next transaction succeeds. With
`manual`, the supervisor waits for an operator:

```bash
build-limits-recover --wait --wait-timeout 300
```

The command opens the transaction even while compiler groups that use the
managed cache are running or draining, but the stop waits for them, because
stopping the cache would interrupt them; until then the transaction reports
`cache recovery stopping` with the reason as its last fault. Groups composed
without the cache do not hold the stop, and every request admitted while a
transaction runs is composed that way, so a busy queue cannot hold it off. The
command refuses outright only while the user manager's transport is faulted,
because a service whose state cannot be read must not be stopped. A supervisor
in `direct` cache mode manages no cache, and one in `kache` mode manages no
service, so both answer with `no-cache-recovery`; see
[Choose a cache backend](#choose-a-cache-backend).

`--wait` polls the status document every 2 seconds for up to `--wait-timeout`
seconds (300 by default). A failed step is retried after 30 seconds, then 60,
and so on in 30-second steps up to 120 seconds apart; the eighth consecutive
failure logs guidance for the phase. None of these numbers is a deadline for
the accounting (the roots grace above only decides when an automatic stop may
be issued, and the verified stopped-and-empty service still releases every
token): a refused stop is retried for as long as the fault persists, and
accounting waits for as long as the drain probe needs. Only the restart gives
up: after eight failed attempts the transaction goes idle with the tokens
already released and the last fault reported.

| Exit status | Meaning                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| `0`         | The transaction finished and no live cache work retains a token                |
| `1`         | The transaction is still running, was blocked, timed out or ended with a fault |
| `2`         | The supervisor could not be asked, so the transaction's state is unknown       |

_Table 6: `build-limits-recover` exit statuses._

Verify the outcome from the supervisor, not from elapsed time:

The `held`, `drain_fault`, `drain_fault_kind` and `caching` fields below are
cache-recovery diagnostics, not generic admission readiness. For provisioning
or general monitoring, use `build-limits-status --ready` or check
`.admission.ready` in the JSON document. A successful status query or `healthy`
alone reports process and pool health, not usable admission. Keep health-only
checks where process health is the intended signal.

```bash
build-limits-status --json | jq '{held, drain_fault, drain_fault_kind, caching}'
```

### What to do when status shows a retained drain

Read the `cache recovery` line and the label of the action line first.

- `cache recovery idle: automatic recovery may start in Ns` with a
  `fallback recovery action:` line means the supervisor will open the recovery
  itself in at most `N` seconds. Keep waiting; do not escalate and do not run
  `build-limits-recover` yet. Escalate only if the line disappears with the
  drain still retained and requests still waiting, or the countdown reaches
  zero without a `cache recovery quiescing` line appearing.
- `cache recovery quiescing`, `stopping`, `accounting` or `restarting` is the
  recovery under way. Keep waiting while its attempts stay below eight.
- `required recovery action:` is a real instruction. It appears when waiting
  cannot help: the `manual` policy, a faulted user manager, a stopped cache
  that is not empty, or a transaction that used its eight attempts. A refused
  restart keeps its `cache recovery last fault:` line.

The `--json` document backs the lines. Under `cache_recovery`, `automatic` says
whether the cache mode and policy let the trigger act, `opens_in` is the
seconds the idle trigger still waits on time alone (`null` when waiting will
not open it, or a transaction is running), and `pending` says a recovery is due
or under way without an operator. At the top level
`recovery_action_is_fallback` is true when the text of `recovery_action` calls
the command a fallback. The journal says
`drain recovery is pending automatically; operator action is only a fallback`
in that case, and `drain requires operator action` otherwise.

Operators upgrading from a supervisor that printed
`required recovery action: Run build-limits-recover …` for every live-work
fault should expect the label and text above for the automatic policy. The
countdown line itself is unchanged, so instructions that tell agents to keep
waiting on it need no edit; instructions that escalate on a live-work fault
with no `cache recovery` line are now rarely triggered, since that picture only
remains for faults that waiting will not cure.

Success is `drain_fault: null` and `drain_fault_kind: null`. A waiting request
can take a released token at once, so `held` may settle above zero; that is the
queue moving again. `caching: false` only says the last reachability probe
found no server; a cache that stays down costs cache hits, not correctness.

Read the fault kind before acting, because stopping the cache is the right
answer to only one of them:

| `drain_fault_kind`  | Meaning                                                     | Action                                                                                                                                                                                |
| ------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live_work`         | A live cache may retain detached work                       | Under `starved`, wait while a countdown or `fallback recovery action:` shows; otherwise run `build-limits-recover`, which stops the cache and proves it empty before releasing tokens |
| `stopped_populated` | The cache is stopped but its control group still holds work | Inspect the surviving processes and wait for them to exit; tokens stay held until then                                                                                                |
| `manager`           | The cache's state could not be read at all                  | Restore the systemd user-manager transport. Do not stop or restart the cache                                                                                                          |

_Table 7: Drain fault kinds and their remedies._

Stopping sccache by hand releases no token, and neither does restarting the
supervisor; only the accounting does. While a transaction runs, a manual
`systemctl --user restart buildlimits-sccache.service` cannot bring the cache
back early, because the unit skips the start while the barrier exists. If the
barrier itself cannot be removed, the transaction stays in its restart phase
until its attempts run out; restore the runtime directory and run
`build-limits-recover` again rather than starting the cache by hand.

A supervisor that dies mid-transaction leaves the barrier behind. Its next
start-up attempts the same accounting: success clears the barrier and starts
the cache, while failure keeps the barrier and refuses to open the pool.

## Metrics

The supervisor and adapters write bounded metric records without request
identifiers to the native journal. `build-limits-metrics` reads them back for a
time window:

```bash
build-limits-metrics --since 1h --max-records 10000 --source all
build-limits-metrics --since 1d --source supervisor --json
journalctl --user BUILD_LIMITS_SCHEMA=1 -o json
```

| Option          | Default | Meaning                                                    |
| --------------- | ------- | ---------------------------------------------------------- |
| `--since`       | `1h`    | The window, as a journalctl relative time such as `1d`     |
| `--max-records` | `10000` | The record cap, from 1 to 100,000; reaching it is reported |
| `--source`      | `all`   | `all`, `supervisor` or `adapter`                           |
| `--json`        | off     | Emit the report, with bounded event history, as JSON       |

_Table 8: `build-limits-metrics` options._

Each query has a 30-second deadline and a 64 MiB output limit; exceeding either
is a query failure, never a partial report. The command exits 0 with a report,
1 when the journal cannot be read, and 2 for invalid options. An empty readable
window succeeds with a warning.

Reports include terminal request rates, outcome and reason counts, nearest-rank
p50, p90, p99 and maximum wait and runtime, queue-depth and held token
summaries, cache-path counts and the direct-fallback ratio. They also summarize
reachability and health transitions, pool failures, supervisor starts, and
adapter outcomes and connection attempts.

Adapter outcomes include `exempt` for a recognized toolchain query. The adapter
records that outcome before exec without wait or runtime fields, so exempt
queries add no samples to the admission wait or runtime distributions.

When outcome telemetry exists in the selected window, the report includes
observed cache-result counts, attribution coverage, reading reasons and skipped
or failed readings, plus store-operation and discard counts. No outcome section
data is inferred for an empty window.

The archive command's `archive_run` record carries the firing's status and
reason, its archived, trimmed, deleted and quarantined action counts, the
number of failed sequences and the elapsed time. It is a closed-vocabulary
adapter record and names no path.

The supervisor also writes OpenTelemetry `scope_placement` spans to the native
journal. An adapter writes `adapter_restart_retry` only when it has to retry
because the supervisor is restarting or unavailable. Query spans separately with
`journalctl --user BUILD_LIMITS_TRACE_EVENT=span_completed -o json`; the
metrics command ignores these records. Span fields describe the operation,
elapsed time, bounded outcome and failure category, known stop mode and retry
attempt. They do not include compiler arguments, paths or raw errors.

When the window holds pressure observations, the report adds a `pressure`
section: the latest descriptor retention and budget, lifetime descriptor and
queue high-water values, listener pauses, drain progress and adapter busy
retries. High-water values are lifetime maxima of the producer, not maxima for
the window. Drain attempt, failure and recovery counters are cumulative
snapshots, so the report shows the largest value observed rather than a sum.

The supervisor terminates and empty-checks every finished group, so drain
progress records only what changes: every failure, pending live work and
recovery, the first success after a different state, and at most one record of
repeated successes per operation every 10 minutes. A window's drain observation
counts therefore exclude routine successes, while the cumulative counters on
each record still include them.

### Supervisor gauge snapshots

A request that has to wait no longer writes a record of its own, and neither
`request_admitted` nor `request_finished` repeats a supervisor-wide count. The
queue depth, held, running, draining and server-drain tokens, capacity and the
descriptor budget arrive on a periodic `gauges` record instead: at most once
every 30 seconds, earlier when one of those counts moves by four or more and at
least five seconds have passed, and once more immediately before the supervisor
stops. The record also carries the interval's queue and held-token peaks, the
lifetime queue high-water mark, the interval it covers and the integral of
queue depth over that interval.

The report's queue summary uses those snapshots. `queue_time_weighted_mean` is
the queue depth averaged over the time each snapshot describes, and
`gauge_coverage_seconds` is the sum of the whole intervals those snapshots
describe. Intervals are not clipped to the query window, so a snapshot whose
interval began before the window counts in full and the reported coverage can
exceed the window's length. A snapshot whose queue depth could not be read
throughout its interval carries no integral: it still counts towards the
coverage but not towards the mean, so an unread gap cannot pull the average
down. The coverage figure therefore measures the intervals the snapshots
describe, not the share of them that fed the mean: do not read
`gauge_coverage_seconds` alone as the measured coverage of
`queue_time_weighted_mean`, because intervals without an integral inflate it.
The maximum queue and held depths take the largest value seen in either shape,
so a window that spans the upgrade keeps describing what an operator saw before
it. `queue_sample_mean` remains for compatibility and is computed only from
records that still carry a per-record sample, such as the rare transition
records that keep explicit gauges. An ordinary new-format window therefore
reports `null` there, and the time-weighted mean is the figure to read.

Deploy the new reader before the new supervisor: an older reader treats
`gauges` as an unknown event and drops it. The migration guide stages the new
release in its own tool directory, so its reader can be checked against the
live journal without restarting or replacing the running supervisor.
`request_queued` is still accepted when reading, so old history remains
readable, but the supervisor no longer writes it.
[Migrating to gauge snapshots](migrating-to-gauge-snapshots.md) gives the
upgrade order and the measured volume change, and
[ADR 009](adrs/adr-0009-supervisor-gauge-snapshots.md) records the decision.

### Routine journal lines

By default the supervisor does not write a plain line for each admission or
each clean finish: the metric records already hold the wait, runtime, outcome
and cache path. Failed, signalled and unstartable compiles are always written.
To see every admission and finish again, for example while following one
group's request and group identifiers, set the variable in a drop-in and
restart the supervisor at a quiet point:

<!-- tested-example: buildlimits-routine-lines-drop-in -->
```ini
[Service]
Environment=BUILD_LIMITS_ROUTINE_LINES=1
```

The routine lines are then written at debug priority, so
`journalctl --user --unit buildlimits-supervisor.service --priority info` still
hides them.

Supervisor records are trusted only when journald supplies
`_SYSTEMD_USER_UNIT=buildlimits-supervisor.service`; adapter records are
self-reported. Journal rate limits and retention can lose history, and the raw
`journalctl` query above applies none of the command's trust filtering.

## Change the capacity or disable admission

Change capacity only at a quiet point. Confirm with `build-limits-status` that
no tokens are held, no requests wait and no groups run, and keep builds
quiescent throughout. Stop the managed cache first, then the supervisor;
replace the pool only after no process holds the old device; then start the
supervisor before the cache. The supervisor's readiness notification orders the
cache start after its start-up accounting.

To disable admission, restore Cargo's previous `rustc-wrapper` and `rustdoc`
settings and restart the launchers that hold the old environment. Leave the
supervisor running until those processes have gone, so that no compiler still
pointing at the adapters is stranded.

## Check the admission model

Admission behaviour over time — draining the cache while a queue is busy,
releasing a retained token, noticing a fault — is checked by a FizzBee model
rather than by the state-machine and property tests that cover the runtime.
Nothing about an installed buildlimits changes: the model reads the design and
produces no user-visible behaviour, and the wheel still contains only
`buildlimits`.

### Prerequisites

Model checking runs on x86-64 Linux, because the pinned release asset is
`fizzbee-$(FIZZBEE_VERSION)-linux_x86.tar.gz` and the recipe unpacks that name
from the archive. The Python baseline is 3.14, the same as the package's:
`make model-check` depends on `make build`, which creates `.venv` and installs
the development dependencies with `uv`. Run

```bash
make build
```

once in a checkout that does not have `.venv`, and install `uv` beforehand if
it is absent.

The installer itself needs GNU `make`, `curl`, `tar` with gzip support,
`sha256sum` and `flock` from util-linux. A network connection is needed only
the first time, unless the cache entry is missing or `FIZZBEE_VERSION` or
`FIZZBEE_SHA256` changes. Fetch and verify the binary with:

```bash
make fizzbee
```

The target downloads the release named by `FIZZBEE_VERSION`, checks it against
`FIZZBEE_SHA256`, and unpacks it into a content-addressed directory under the
user cache. It then links `.tools/fizzbee` at the worktree root. A download
that fails, or a digest that does not match, installs nothing: the partial
asset is removed, no cache entry is written, and the link is not created. A
cache entry and link left by an earlier successful install are not removed, so
a later run still finds the last verified checker rather than a partial one.
The binary is never committed and never installed system-wide; `make test`,
`make lint`, `make typecheck` and `make check-fmt` never fetch or run it.

### Run the checks

Two targets cover the two obligations, because they cost very different amounts.

```bash
make model-fidelity
```

runs in seconds. It compares the model's `# maps:` annotations with the
production package and reports any admission event, effect, recovery phase or
fault kind the model does not account for. It runs on every pull request.

```bash
make model-check
```

runs the full matrix in both model halves and takes minutes. Each
configuration's expectations are judged, and `--fixpoint` re-runs every
configuration that should pass at twice its declared `max_actions` bound, so a
run truncated at its bound is reported rather than passed. Unequal
doubled-bound counts do not by themselves prove a production defect: they mean
the bound is too low or the exploration has not converged, and the model needs
a bound that reaches its fixpoint before its expectations can say anything
about the code. It runs nightly on GitHub runners and can be started by hand
through `workflow_dispatch`.

Both targets are wrappers. The tool itself is available directly, through
`uv run` so it uses the environment `make build` created:

```bash
uv run python -m modelcheck check models/admission/coupling --fizz .tools/fizzbee/fizz
uv run python -m modelcheck check models/admission/transaction --fizz .tools/fizzbee/fizz
uv run python -m modelcheck drift models/admission/coupling
```

`check` prints one line per expectation, naming the configuration, the
property, the verdict, the fairness assumptions in force and the states
explored. `--only CONFIG[:PROPERTY]` narrows the report and the matrix traces
`--save-traces` writes, without narrowing the check, the exit status or the
fixpoint comparison; a failed comparison's trace is kept for every
configuration, whatever `--only` selected. `--bounds routine` selects the bound
set a model declares. Routine is the only set implemented today: any other
value is refused with status `2` rather than falling back to the routine
bounds, because a run that checked different bounds from the ones it named
would be reported as the run asked for. `--json PATH` writes the judgements as
JSON and `--save-traces` keeps each counterexample's normalized trace under the
model's `traces/` directory.

### Exit statuses

Both commands distinguish a wrong model from a tool that could not run:

| Status | Meaning                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------- |
| `0`    | Every expectation was met, or `drift` found a clean report.                                       |
| `1`    | An expectation was unmet, a fixpoint comparison grew, or `drift` found an unmapped or stale path. |
| `2`    | The tool could not run: a usage error or an input it cannot interpret.                            |

`check` and `drift` ask different questions but share the statuses. `check`
reports `0` when every configuration meets its expectations; `drift` reports
`0` when the model accounts for every production path it names, and `1` when
the report names a path the inventory does not hold (stale) or an inventory
item the model neither maps nor excludes (unmapped). Either command reports `2`
only when the tool could not run at all.

A `2` is never folded into `1`, and the boundary in the other direction is just
as important. A `2` means the command could not complete and says nothing about
the model; it can be raised after the checker has already run, for example when
the path `--json` names cannot be written. A checker that never started is
different: the command did run, so the matrix records an error judgement naming
the reason and the status is `1`. So is a counterexample whose error graph
exists but cannot be read or parsed. `--fixpoint` changes the second case: the
comparison re-reads each configuration at twice its bound and raises when a run
that should pass has not, so the report is printed and the same missing checker
then ends the command with `2`. A script that treats `1` and `2` differently
should therefore not assume a checker failure keeps the status at `1` when the
fixpoint comparison is asked for. Only `0` is a pass; a job that treated any
other status as one would report green for a model that was never checked.

### Generated artefacts

A run copies its specification into a fresh directory under the git-ignored
`.model-check/` and writes its output there, so the run itself writes no
tracked file. `make model-check` also writes `.model-check/coupling.json` and
`.model-check/transaction.json`, which the end-to-end tests read. It passes
`--save-traces` as well, so each counterexample's normalized path is kept under
`models/admission/<half>/traces/`. That directory is tracked: the seven
`head-*.json` traces are committed evidence for the incidents the model
reproduces, and a run rewrites the ones it produces rather than adding
untracked files beside them. A trace for a configuration or stage that has no
committed file yet is new, so it shows up as untracked until it is reviewed and
committed. A failed comparison's counterexample is kept the same way, named
`<configuration>-fixpoint-<stage>.json` so it sits beside the matrix's trace
for the same configuration rather than replacing it. A trace is written only
for a counterexample, so a configuration that once failed and now passes keeps
its old file until a maintainer deletes it: the run replaces the traces it
writes and removes nothing else. It also keeps an existing trace when the new
one carries evidence redaction had to withhold: a one-character credential in
the run's environment withholds every decoded state string containing that
character, so such a trace names fewer variables than the model declares and
would replace evidence about the model with evidence about the host. The run
prints the file it left alone, and unsetting that variable lets a later run
record the trace whole. The JSON record names the kind of result each failed
run produced, and a run that reported no exploration time of its own is
recorded with the time the harness waited for it, under a field of its own.
Each captured text in a record's `detail` — the checker's transcript, the graph
when it could not be decoded, or a stopped run's marker line — is cut to 4,000
characters with the cut stated, so one record can hold two captures. The run's
own paths and the values of credential-named environment variables are replaced
before the text is kept, and again in the trace's states once the graph is
parsed, so a committed artefact describes the model rather than the machine
that checked it. The graph is read before any of that happens, so a replacement
cannot turn a valid graph into an unparsable one or make two states compare
equal. Credentials shorter than six characters are replaced only where they
stand alone, and a one-character value is replaced only where it is a value —
after `=`, between quotes, after the label a checker prints beside it, or as a
whole decoded state value — so a boolean flag cannot rewrite every state count.
A decoded state string that still contains a one-character credential is
withheld rather than recorded, and state variable names withheld to the same
marker are numbered so that every variable survives under one name throughout
the trace. A one-character credential embedded inside a longer token of free
diagnostic text — `Error: prefix#` for a credential of `#` — is not recognized
and stays in the record. If a credential of two or more characters survives any
replacement, including under an equivalent JSON escape spelling, the text
holding it is withheld rather than recorded and the record says so. Read a
generated artefact before sharing it.

The end-to-end tests that read these artefacts are opt-in because they need the
binary:

```bash
BUILD_LIMITS_MODEL_CHECK=1 uv run pytest -n 0 -m fizzbee tests
```

Model authoring, the split into two halves and the developer's guide's account
of the materializer are in the [developer's guide](developers-guide.md) §11.6.
