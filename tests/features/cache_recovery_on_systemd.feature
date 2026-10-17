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
    And a later build waits for a token that is never free
    When the operator recovers the shared admission and waits for it
    Then the recovery completes and frees both held tokens
    And the waiting build succeeds
    And the cache was restarted after its accounting
    And the cache refuses to start again while the restart barrier is present
