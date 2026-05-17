Feature: In-process embed cache for repeated query text
  As an AI agent calling tool_search in a multi-agent teaming session
  I want repeats of a recently-embedded query text to skip the Azure embed roundtrip
  So that the next agent asking the same question doesn't pay the ~250-500 ms latency I paid

  Background:
    Given an embed-text wrapper backed by an in-process embed cache

  Scenario: Identical queries from different agents share the embed cache
    Given a counting embed backend that records every call
    When agent "alpha" embeds the text "FEAT-081 status"
    And agent "bravo" embeds the text "FEAT-081 status"
    Then the embed backend was called only once
    And the embed cache reports one hit and one miss

  Scenario: Whitespace and case differences collapse to the same cache slot
    Given a counting embed backend that records every call
    When some caller embeds the text "  FEAT-081 status  "
    And some caller embeds the text "feat-081 STATUS"
    Then the embed backend was called only once

  Scenario: Empty queries do not pollute the cache
    Given a counting embed backend that records every call
    When some caller embeds an empty text
    And some caller embeds a whitespace-only text
    Then the embed cache contains zero entries
    And the embed backend was not called
