Feature: Transport coalesces concurrent embed requests into batched provider calls
  As a kairix operator running parallel retrieval workloads
  I want concurrent embed requests within a short time window to fold
  into a single batched provider call
  So that we make one HTTP round-trip per window instead of N — without
  callers needing to know batching exists

  # Test seam: a FakeProvider from tests/fakes.py that records each
  # embed_batch invocation and the size of the texts list it received.
  # The coalescer's job is to fan-in many caller requests into a few
  # provider invocations.

  Background:
    Given a fake provider that records each batched embed call it serves
    And a transport coalescer wrapping the fake provider

  @happy_path
  Scenario: Ten concurrent requests in one window collapse into one batched call
    Given the coalescer is configured with a 50 millisecond window and max batch size 32
    When 10 callers concurrently request embeddings within the same 50 millisecond window
    Then every caller receives a non-empty embedding vector
    And the fake provider records exactly 1 batched embed call
    And the batched embed call carried 10 texts
    # Sabotage: if a future change disables coalescing or collapses the
    # window to zero, each caller would dispatch alone — the recorded
    # call count would be 10, not 1, and the per-call text count would
    # be 1, not 10.

  Scenario: A lonely request waits up to the window then dispatches alone
    Given the coalescer is configured with a 50 millisecond window and max batch size 32
    When 1 caller requests an embedding and no other callers arrive
    Then the caller receives a non-empty embedding vector within 100 milliseconds
    And the fake provider records exactly 1 batched embed call
    And the batched embed call carried 1 text
    # Sabotage: if the coalescer never flushes a partial window, the
    # caller would hang past the timeout and this scenario would fail.
    # If the coalescer flushes instantly with no window, the latency
    # assertion would still pass, so we also assert the call count to
    # catch a "coalescer disabled" regression.

  Scenario: Seventeen concurrent requests with max batch sixteen split into two batches
    Given the coalescer is configured with a 50 millisecond window and max batch size 16
    When 17 callers concurrently request embeddings within the same 50 millisecond window
    Then every caller receives a non-empty embedding vector
    And the fake provider records exactly 2 batched embed calls
    And one batched embed call carried 16 texts
    And one batched embed call carried 1 text
    # Sabotage: a coalescer that ignores max_batch_size would record
    # 1 call of 17 texts (and provider-side rate limits would later
    # reject it in production). A coalescer that flushes per request
    # would record 17 calls of 1 text.

  Scenario: A coalescer with a zero window dispatches every request synchronously
    Given the coalescer is configured with a 0 millisecond window and max batch size 32
    When 5 callers concurrently request embeddings
    Then every caller receives a non-empty embedding vector
    And the fake provider records exactly 5 batched embed calls
    And every batched embed call carried 1 text
    # Sabotage: a zero-window coalescer that still tries to batch would
    # record fewer than 5 calls — catching the case where "disable
    # coalescing" silently doesn't.

  Scenario: Distinct windows do not merge into a single batched call
    Given the coalescer is configured with a 20 millisecond window and max batch size 32
    When 3 callers concurrently request embeddings within the same 20 millisecond window
    And after the window closes 3 more callers concurrently request embeddings
    Then every caller receives a non-empty embedding vector
    And the fake provider records exactly 2 batched embed calls
    And each batched embed call carried 3 texts
    # Sabotage: a coalescer that holds the buffer open across windows
    # would record 1 call of 6 texts; one that flushes per request
    # would record 6 calls of 1 text.
