Feature: Compile admission on a real systemd user manager
  The supervisor runs as a user service, launches each admitted compiler in a
  transient scope, and accounts for every token across cancellation, crashes
  and cached compilation.

  Scenario: Builds beyond capacity wait for a slot
    Given a supervisor with 2 slots
    When 5 builds run concurrently
    Then every build succeeds
    And no more than 2 compilers ever ran at once

  Scenario: Streams and exit status are preserved
    Given a supervisor with 1 slot
    When a build reads its input, writes to both streams and exits with 7
    Then the adapter exits with 7
    And the build's streams are intact

  Scenario: A compiler killed by a signal ends the adapter the same way
    Given a supervisor with 1 slot
    When a build's compiler is killed by SIGTERM
    Then the adapter is killed by SIGTERM

  Scenario: The caller's umask reaches the compiler
    Given a supervisor with 1 slot
    When a build creates a file under umask 077
    Then the file is readable by its owner only

  Scenario: A cancelled running build is terminated with its descendants
    Given a supervisor with 1 slot
    When a build starting a background process is cancelled while running
    Then the compiler and its background process are terminated
    And the slot is free again

  Scenario: A cancelled waiting build never starts
    Given a supervisor with 1 slot
    When a second build is cancelled while it waits for the first
    Then the second compiler never ran
    And the first build still succeeds

  Scenario: A nested adapter reuses its group's admission
    Given a supervisor with 1 slot
    When a build's compiler runs the adapter again
    Then the nested compiler runs without waiting

  Scenario: Processes left behind by a compiler are removed
    Given a supervisor with 1 slot
    When a build's compiler leaves a background process and exits
    Then the build succeeds
    And the background process is terminated

  Scenario: A restarted supervisor terminates surviving compilers first
    Given a supervisor with 1 slot
    When the supervisor crashes while a build compiles
    And the supervisor starts again
    Then the surviving compiler has been terminated
    And a new build succeeds

  Scenario: A killed cached build holds its slot while the server compiles
    Given a supervisor with 1 slot composing builds with sccache
    When a cached build is cancelled while the server compiles it
    Then the server is still compiling
    And the slot is still held
    When the server's compilation finishes
    Then the slot is still held
    When the managed sccache server stops
    Then the slot is free again

  Scenario: A build compiles without the cache when the server is down
    Given a supervisor with 1 slot composing builds with sccache
    When the managed sccache server stops
    And a build runs while the server is down
    Then the build was not started through the sccache client
    And the build's standard error says the cache is unavailable
