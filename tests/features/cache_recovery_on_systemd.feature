Feature: Shared compile admission recovers from cached work that outlived its requester
  A cancelled requester can leave accepted work detached in the managed cache.
  Its token must stay held while that work is unaccounted for, and only the
  operator's own recovery may release it, after the cache is proved stopped
  and empty. A request still waiting for a token must keep its place and its
  cancellation throughout.

  Scenario: Two cancelled cached requesters are released only by verified recovery
    Given a supervisor with 2 slots composing builds with sccache
    And a fixture jobserver the managed cache waits on before it spawns a compiler
    When two cached builds wait for a compiler slot before any compiler spawns
    And both builds are killed while they wait
    Then both killed builds' tokens stay held with no compiler running
    And manual recovery is required for admission readiness
    And a later build waits for a token that is never free
    And manual recovery remains required after a build queues
    When the operator recovers the shared admission and waits for it
    Then the recovery completes and frees both held tokens
    And admission readiness is restored
    And the waiting build succeeds
    And the cache was restarted after its accounting
    And the cache refuses to start again while the restart barrier is present

  Scenario: Starved policy stays ready while it waits for demand
    Given a supervisor with 2 slots using starved recovery and sccache
    And a fixture jobserver the managed cache waits on before it spawns a compiler
    When two cached builds wait for a compiler slot before any compiler spawns
    And both builds are killed while they wait
    Then both killed builds' tokens stay held with no compiler running
    And automatic recovery waits for demand and admission remains ready
    When a later build arrives to trigger automatic recovery
    Then automatic recovery clears the drain fault and restores readiness
    And the waiting build succeeds
    And the cache was restarted after its accounting

  Scenario: Recovery opens beside a running cached build and stops the cache later
    Given a supervisor with 2 slots composing builds with sccache
    And a fixture jobserver the managed cache waits on before it spawns a compiler
    When two cached builds wait for a compiler slot before any compiler spawns
    And one of the two builds is killed while it waits
    Then the killed build's token stays held while the other build keeps running
    And readiness remains true during ordinary saturation
    When the operator asks for recovery without waiting
    Then recovery opens but leaves the cache running under the live build
    When the surviving build is killed
    And the operator recovers the shared admission and waits for it
    Then the recovery completes and frees both held tokens
    And admission readiness is restored
    And the cache was restarted after its accounting
