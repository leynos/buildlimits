Feature: kache composition on a real systemd user manager
  kache runs the compiler as a child of its wrapper inside the admitted group's
  own scope, and is pointed at a daemon this supervisor never owns. These
  scenarios exercise that shape end to end, including the two ways it degrades:
  an unusable kache, and a kache process no admitted scope accounts for.

  Scenario: A kache build runs inside its group's scope and returns its token
    Given a supervisor with 1 slot composing builds with kache
    When a build runs through kache
    Then the compiler ran inside the build's scope
    And the kache daemon is still running
    And the slot is free again

  Scenario: A cancelled kache group leaves no processes behind
    Given a supervisor with 1 slot composing builds with kache
    When a kache build starting a background process is cancelled
    Then no process from that group survives
    And the kache daemon is still running
    And the slot is free again

  Scenario: A missing kache executable compiles directly with a notice
    Given a supervisor with 1 slot composing builds with kache but no kache installed
    When a build runs while kache is unavailable
    Then the build was not started through the kache client
    And the build's standard error says kache is not serving compilations
    And the build admits one direct fallback

  Scenario: A kache process outside every admitted scope is reported
    Given a supervisor with 1 slot composing builds with kache
    And a kache process parked outside the admitted scopes
    Then status reports that stray process
    And the supervisor's journal names that stray process
    When a build runs while the stray is parked
    Then the build still gets a slot
