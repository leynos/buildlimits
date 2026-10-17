Feature: Descriptor pressure under real compiler admission
  Compiler requests retain their high target descriptors and recover all tokens
  even while one admitted compiler creates a concurrent queue.

  Scenario: Dense inherited descriptors recover after queued and running cancellation
    Given one compiler slot with soft limit 128 and a reachable hard limit
    When six dense requests with target descriptor 255 queue concurrently
    And one queued requester and the running requester disconnect
    And the remaining compilers finish
    Then every surviving requester succeeds with its target descriptor preserved
    And compiler tokens and supervisor descriptors return to baseline
