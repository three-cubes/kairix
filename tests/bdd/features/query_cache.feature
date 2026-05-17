Feature: In-process query-result cache for repeated agent queries
  As an AI agent calling tool_search in a teaming session
  I want repeats of a recently-asked query to return without re-doing the embed roundtrip
  So that the second agent asking the same question doesn't pay the latency I paid

  Background:
    Given a search pipeline wired with an in-process query cache

  Scenario: First call misses, second identical call hits the cache
    Given a backend that counts how many times it is asked
    When an agent asks the search pipeline for "FEAT-081 status"
    And the same agent asks the search pipeline for "FEAT-081 status" again
    Then the backend was asked only once
    And the cache stats report one hit and one miss

  Scenario: Whitespace and case differences collapse to the same cache slot
    Given a backend that counts how many times it is asked
    When an agent asks the search pipeline for "  FEAT-081 status  "
    And the same agent asks the search pipeline for "feat-081 STATUS" again
    Then the backend was asked only once

  Scenario: Different agents share no cache slot for the same query
    Given a backend that counts how many times it is asked
    When agent "shape" asks the search pipeline for "team sync"
    And agent "builder" asks the search pipeline for "team sync"
    Then the backend was asked twice

  Scenario: Errors returned by the search pipeline are not cached
    Given a pipeline configured to produce an error envelope
    When an agent asks the search pipeline for "anything"
    Then the cache contains zero entries
    And the cache stats report zero hits and one miss
