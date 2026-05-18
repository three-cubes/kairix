Feature: In-process coalescer folds concurrent embed calls into one batched request
  As a kairix operator running a teaming deployment
  I want N agents asking N different questions in the same window to share one Azure roundtrip
  So that embed_http tail latency at concurrency 10 drops from ~1100 ms to ~250 ms

  Background:
    Given an embed coalescer with a counting batch backend

  Scenario: Ten concurrent embed calls collapse to one batched HTTP request
    When ten threads each call the coalescer with their own text in the same window
    Then the embed batch backend was called exactly once
    And the single batch contained ten texts

  Scenario: Sequential embed calls fire immediately within the bounded window
    When a single caller asks the coalescer to embed one text
    Then the call returns within the bounded coalesce window

  Scenario: Empty input bypasses the coalescer entirely
    When some caller asks the coalescer to embed an empty text
    Then the embed batch backend was not called
    And the coalescer reports zero requests
