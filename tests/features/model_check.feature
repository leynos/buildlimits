Feature: Model check verdicts
  The model check passes only when every fixed configuration holds and every
  known-bad configuration still yields the counterexample it was built to find.
  Every other outcome — a lost counterexample, a counterexample of the wrong
  kind, an exploration cut short, uninterpretable output, an unsatisfied probe —
  is a failure, because a check that cannot fail proves nothing.

  Background:
    Given a model check over a model directory

  Scenario: A fixed configuration holds
    Given a configuration "head" expected to satisfy "L3"
    And the checker output is the fixture "pass"
    When the model check runs
    Then the report shows "head" "L3" as "holds"
    And the model check exits with status 0

  Scenario: A fixed configuration fails
    Given a configuration "head" expected to satisfy "S1"
    And the checker output is the fixture "safety-failure"
    When the model check runs
    Then the report shows "head" "S1" as "unexpected counterexample"
    And the model check exits with status 1

  Scenario: A known-bad configuration yields its counterexample
    Given a configuration "incident-1" expected to fail "L3" with a lasso
    And the checker output is the fixture "lasso"
    When the model check runs
    Then the report shows "incident-1" "L3" as "counterexample as expected"
    And the model check exits with status 0

  Scenario: A known-bad configuration loses its counterexample
    Given a configuration "incident-1" expected to fail "L3" with a lasso
    And the checker output is the fixture "pass"
    When the model check runs
    Then the report shows "incident-1" "L3" as "expected counterexample missing"
    And the model check exits with status 1

  Scenario: A counterexample of the wrong kind does not count
    Given a configuration "incident-3" expected to fail "L4a" with a finite trace
    And the checker output is the fixture "lasso"
    When the model check runs
    Then the report shows "incident-3" "L4a" as "wrong counterexample kind"
    And the model check exits with status 1

  Scenario: A truncated exploration is not a pass
    Given a configuration "head" with a floor of 10 states expected to satisfy "S2"
    And the checker output is the fixture "truncation-safety"
    When the model check runs
    Then the report shows "head" "S2" as "state space below floor"
    And the model check exits with status 1

  Scenario: Uninterpretable checker output fails closed
    Given a configuration "head" expected to satisfy "S1"
    And the checker output has no verdict line
    When the model check runs
    Then the report shows "head" "S1" as "error"
    And the model check exits with status 1

  Scenario: An exploration below its floor is not a pass
    Given a configuration "head" with a floor of 1000 states expected to satisfy "S1"
    And the checker output is the fixture "pass" with 300 states explored
    When the model check runs
    Then the report shows "head" "S1" as "state space below floor"
    And the model check exits with status 1

  Scenario: An unsatisfied reachability probe fails
    Given a configuration "head" expected to reach "StopIssued"
    And the checker reports that "StopIssued" was never satisfied
    When the model check runs
    Then the report shows "head" "StopIssued" as "unreachable"
    And the model check exits with status 1

  Scenario: Every report names its fairness assumptions
    Given a configuration "head" expected to satisfy "L1"
    And the model tags fairness "F1, F3, F6" on the actions "L1" relies on
    And the checker output is the fixture "pass"
    When the model check runs
    Then the report line for "head" "L1" names "F1, F3, F6"

  Scenario: An unmapped effect is drift
    Given the model maps every admission event and effect
    And the code gains an effect "Pause"
    When the drift check runs
    Then it fails naming "buildlimits.effects.Pause"

  Scenario: A renamed mapped function is drift
    Given the model maps "buildlimits.admission.Admission.token_arrived"
    And that path no longer resolves
    When the drift check runs
    Then it fails naming "buildlimits.admission.Admission.token_arrived"
