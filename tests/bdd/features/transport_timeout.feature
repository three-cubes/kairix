Feature: Transport enforces per-request timeouts without leaking resources
  As a kairix operator whose provider can slow to a crawl during incidents
  I want the transport layer to bound each request by a per-call timeout
  and reclaim any sockets or file descriptors when the timeout fires
  So that one slow provider can't park caller threads indefinitely, and
  repeated timeouts don't exhaust the process's file-descriptor budget

  # Test seam: a FakeProvider from tests/fakes.py that delays each
  # response by a programmable interval and exposes counters for
  # "sockets opened" and "sockets closed". The transport timeout policy
  # wraps the provider.

  Background:
    Given a fake provider that delays each response by a programmable interval
    And a transport timeout policy wrapping the fake provider with a 200 millisecond per-request timeout
    And a fixture-tracked socket counter starting at zero

  @happy_path
  Scenario: A provider that responds within the timeout returns its result
    Given the fake provider is configured to delay each response by 50 milliseconds
    When the caller dispatches one embed request
    Then the caller receives a successful embed response within 200 milliseconds
    And the fixture-tracked socket counter shows opened equals closed
    # Sabotage: a transport that opens a socket per attempt but never
    # closes it would show opened > closed even on success — catching
    # the fd-leak class on the happy path.

  Scenario: A slow provider raises TimeoutExceeded to the caller
    Given the fake provider is configured to delay each response by 1000 milliseconds
    When the caller dispatches one embed request
    Then the caller sees a TimeoutExceeded error within 250 milliseconds
    And the TimeoutExceeded error reports the configured 200 millisecond budget
    # Sabotage: a transport that swallows slow responses (no timeout
    # enforcement) would block the caller for the full 1000 ms or
    # forever. The 250 ms assertion is tight enough to catch a no-op
    # timeout and loose enough to allow scheduler jitter.

  Scenario: A timeout fires and releases the underlying socket
    Given the fake provider is configured to delay each response by 1000 milliseconds
    When the caller dispatches one embed request
    Then the caller sees a TimeoutExceeded error
    And the fixture-tracked socket counter shows opened equals closed
    # Sabotage: the original fd-leak class — a cancelled request leaves
    # its socket open and the counter shows opened > closed. This
    # scenario pins the release contract on the timeout path.

  Scenario: Repeated timeouts do not accumulate leaked file descriptors
    Given the fake provider is configured to delay each response by 1000 milliseconds
    When the caller dispatches 20 embed requests each timing out
    Then every caller sees a TimeoutExceeded error
    And the fixture-tracked socket counter shows opened equals closed
    And the fixture-tracked socket counter peak open value never exceeds 1 per concurrent request
    # Sabotage: a cumulative leak of one socket per timeout would show
    # opened = 20, closed = 0; a partial leak would show opened > closed
    # by some constant. The peak-open assertion catches the case where
    # cleanup is deferred past the visible counter window.

  Scenario: Timeout policy can be tightened per call without rebuilding the transport
    Given the fake provider is configured to delay each response by 500 milliseconds
    When the caller dispatches one embed request with a 100 millisecond per-call timeout override
    Then the caller sees a TimeoutExceeded error within 200 milliseconds
    And the TimeoutExceeded error reports the override 100 millisecond budget
    # Sabotage: a transport that ignores the override and uses the
    # background 200 ms budget would either succeed (provider responds
    # at 500 ms — actually no, would also time out, but on the wrong
    # budget) — the budget-reported value pins which budget actually
    # fired.

  Scenario: A timeout during a coalesced batch fails each caller in the batch
    Given the transport timeout policy is composed with a 50 millisecond coalescer window
    And the fake provider is configured to delay each response by 1000 milliseconds
    When 5 callers concurrently request embeddings within the same 50 millisecond window
    Then every caller sees a TimeoutExceeded error
    And the fake provider records exactly 1 batched embed call attempted
    And the fixture-tracked socket counter shows opened equals closed
    # Sabotage: a transport that fails only the first caller and hangs
    # the rest (a real bug class with shared futures) would leave 4
    # callers waiting forever; the scenario fails on either the
    # per-caller error assertion or the leak-free socket assertion.
