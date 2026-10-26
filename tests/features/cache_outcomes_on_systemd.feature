Feature: Recording cache outcomes under a real user manager
  Background:
    Given an isolated supervisor with cache_outcomes "record" and a scripted fake sccache

  Scenario: Labels follow the script
    Given the fake sccache scripts "miss" then "hit"
    When two cached compiles run one after the other with nothing queued
    Then the journal has cache_outcome events "miss" then "hit" with attribution "solo"
    And the outcome store holds two records with the same key
    And the metrics report agrees with the fake cache
    And the H3 replay reports two attributed outcomes as insufficient

  Scenario: No query runs while the restart barrier exists
    Given the cache recovery barrier is present
    When a cached compile finishes
    Then no statistics query was started

  Scenario: The first window after a restart uses the new generation
    Given one attributed compile in the first invocation
    When the cache unit restarts with its previous counters intact
    And one cached compile runs
    Then that compile is attributed against the new generation

  Scenario: A stalled statistics query does not stall admission
    Given the fake sccache answers statistics after 5 seconds
    When a cached compile finishes and another compile is submitted
    Then the second compile is admitted while the query is outstanding
    And an outcome_reading event reports "timeout"

  Scenario: Status shows that recording is on
    When a cached compile is attributed
    Then build-limits-status shows a cache outcomes line with 1 attributed
