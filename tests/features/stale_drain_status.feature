Feature: Status while a stale drain waits for automatic recovery
  An agent reading build-limits-status must be able to tell a recovery that
  is about to happen by itself from one that needs an operator.

  Background:
    Given a busy host retaining one drained token while a build waits

  Scenario: The automatic policy counts down to the stale proof
    When 100 seconds pass
    Then status says automatic recovery may start in 200.0s
    And the recovery action is labelled fallback recovery action

  Scenario: The manual policy asks the operator to act
    Given the recovery policy is manual
    When 100 seconds pass
    Then status shows no recovery countdown
    And the recovery action is labelled required recovery action

  Scenario: The stale proof opens the recovery
    When 300 seconds pass
    Then the recovery transaction has opened
