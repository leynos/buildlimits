Feature: Recording child sightings under a real user manager
  Background:
    Given an isolated supervisor with cache_outcomes "record" and a scripted fake sccache

  Scenario: Child sightings follow the script
    Given the fake sccache scripts "miss" then "hit"
    When two cached compiles with distinct -C metadata run one after the other
    Then the "miss" cache_outcome reports child_sighting "seen"
    And the "hit" cache_outcome reports child_sighting "not_seen"
    And the outcome store holds no raw arguments or metadata values

  Scenario: Shared metadata remains ambiguous
    Given the fake sccache scripts "miss" then "hit"
    When two active requests share metadata while only one has a full-emit child
    Then both cache_outcome records report child_sighting "ambiguous"
    And H2 replay excludes those child sightings from child-only agreement
