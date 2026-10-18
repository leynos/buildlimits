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
