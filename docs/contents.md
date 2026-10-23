# Documentation contents

- [Documentation contents](contents.md) is this index of the buildlimits
  documentation set.

## Guides

- [User's guide](users-guide.md) explains how to install, configure and
  operate compile admission: status, cache recovery, metrics and capacity
  changes.
- [Developer's guide](developers-guide.md) is the maintainer's manual: quality
  gates, the supervisor's internal interfaces, the test helpers and the
  repository's static-analysis exceptions.
- [Repository layout](repository-layout.md) maps every major path to its
  responsibility and conventions.
- [Migrating from the dev-env-rocky copy](migrating-from-dev-env-rocky.md)
  lists what operators must change when moving from `build_limits` in
  dev-env-rocky to this repository's 0.1.0 release.

## Decisions

- [ADR 001: graceful supervisor restart](adr-001-graceful-supervisor-restart.md)
  records why a plain `systemctl --user restart` drains running work, how the
  adapters and start-up cooperate, and the options it rejected.
- [ADR 002: keep a real toolchain compiler's library directory on the loader path](adr-002-toolchain-library-path.md)
  records why the adapters prefix a real `rustc` or `rustdoc`'s own `lib`
  directory to `LD_LIBRARY_PATH`, how it mirrors rustup, and what stays outside
  the mitigation.
- [ADR 003: bounded restart tracing](adr-003-bounded-restart-tracing.md)
  records the local OpenTelemetry spans, bounded journal schema and failure
  isolation used at process boundaries.

## Conventions

- [Documentation style guide](documentation-style-guide.md) defines the
  spelling, structure, Markdown, Architecture Decision Record (ADR), Request
  for Comments (RFC), and roadmap conventions used by this documentation set.
- [Scripting standards](scripting-standards.md) explains the preferred Python
  scripting stack, command execution patterns, and test expectations for helper
  scripts.

## Engineering practice

- [Complexity antipatterns and refactoring strategies](complexity-antipatterns-and-refactoring-strategies.md)
  explains cognitive complexity, the bumpy-road antipattern, and refactoring
  approaches for maintainable code.
- [Local validation of GitHub Actions with act and pytest](local-validation-of-github-actions-with-act-and-pytest.md)
  explains how to validate workflow behaviour locally before relying on remote
  Continuous Integration (CI) runs.

## Planning

- [Roadmap](roadmap.md) sequences the delivery of accepted RFCs as phases,
  steps and tasks with dependencies and success criteria.

## Requests for comments

- [RFC 0001: FizzBee model of compile admission and cache recovery](rfcs/0001-fizzbee-model-of-admission-and-recovery.md)
  proposes a formal model of admission and recovery, its safety and liveness
  properties, conformance checks and a gated adoption plan. Accepted 2026-10-18.
- [RFC 0002: Recording and predicting sccache hits for queued
  compiles](rfcs/0002-cache-hit-prediction.md) proposes recording each cached
  compilation's real outcome, adding an advisory hit prediction to status and
  metrics, and conducting a gated analysis of hit-first admission. Accepted
  2026-10-23.
