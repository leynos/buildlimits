Feature: Admission rules for compiler groups
  The supervisor admits at most one compiler group per pool token, keeps each
  token until its group has gone, and never lets cached work escape the count.

  Background:
    Given an admission core with caching enabled

  Scenario: A build waits while the pool is empty
    When two builds are submitted
    And the pool supplies one token
    Then one compiler group is running
    And one build is waiting without a token

  Scenario: A cancelled waiting build never starts
    When two builds are submitted
    And the second build is cancelled
    And the pool supplies one token
    Then only the first build is launched

  Scenario: A cancelled running build keeps its token until it has drained
    When a build is submitted
    And the pool supplies one token
    And the build is cancelled
    Then its group is terminated
    And the token is still held
    When the group's leader exits and its scope empties
    Then the token is still held
    When the sccache server reports no live compilations
    Then the token is returned

  Scenario: A killed cached build keeps its token while the server compiles
    When a build is submitted
    And the pool supplies one token
    And the group's leader is killed and its scope empties
    Then the build's result is reported
    And the token is still held
    When the sccache server reports one live compilation
    Then the token is still held
    When the sccache server reports no live compilations
    Then the token is returned

  Scenario: A pool failure drains everything and stops
    When two builds are submitted
    And the pool supplies one token
    And the pool fails
    Then the waiting build is refused
    And the running group is terminated
    When the group's leader exits and its scope empties
    And the sccache server reports no live compilations
    Then the supervisor shuts down
