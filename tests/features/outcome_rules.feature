Feature: Attributing sccache outcomes from statistics at quiet points
  A label is given only when the counters can say which request it belongs to.

  Scenario: A solo window that only moved cache_hits is a hit
    Given a baseline reading in generation "a"
    And one cached group that ended cleanly
    When a reading in generation "a" shows cache_hits up by 1 and compile_requests up by 1
    Then the group is labelled "hit" with attribution "solo"

  Scenario: A solo window that only moved compilations is not a hit
    Given a baseline reading in generation "a"
    And one cached group that ended cleanly
    When a reading in generation "a" shows compilations up by 1 and compile_requests up by 1
    Then the group is labelled "not_cached" with attribution "solo"

  Scenario: A mixed window is never split
    Given a baseline reading in generation "a"
    And three cached groups that ended cleanly
    When a reading shows one hit, one miss and one declined request
    Then every group is "unattributed" with attribution "none"
    And the outcome reason is "mixed_window"

  Scenario: A restart is caught even when counters climb past the old values
    Given a baseline reading in generation "a" with compile_requests 5
    And one cached group that ended cleanly
    When a reading in generation "b" shows compile_requests 9
    Then the group is "unattributed" with outcome reason "generation_changed"

  Scenario: A signalled group makes its window ineligible
    Given a baseline reading in generation "a"
    And one cached group that was signalled
    When a reading in generation "a" shows a miss
    Then the group is "unattributed" with outcome reason "ineligible_window"

  Scenario: Cargo's version probe is declined, not a non-cacheable compile
    Given a baseline reading in generation "a"
    And one cached group that ended cleanly
    When a reading shows requests_not_cacheable up by 1 and compile_requests up by 1
    Then the group is labelled "declined" with attribution "solo"
