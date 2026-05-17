Feature: Transport retries transient provider failures and surfaces typed exhaustion
  As a kairix operator whose provider sometimes returns 503 or
  connection-reset under load
  I want the transport layer to retry transient errors with backoff and
  give up cleanly on permanent ones
  So that intermittent provider hiccups don't bubble up to callers, but
  caller errors and exhaustion are still distinguishable to operators

  # Test seam: a FakeProvider from tests/fakes.py whose next-response
  # script can be set to: succeed | raise a transient error | raise a
  # 4xx client error. The provider records each attempt.

  Background:
    Given a fake provider whose response script the test can program
    And a transport retry policy wrapping the fake provider with max 3 attempts

  @happy_path
  Scenario: A provider that succeeds on the first attempt is called once
    Given the fake provider is scripted to succeed on attempt 1
    When the caller dispatches one embed request
    Then the caller receives a successful embed response
    And the fake provider records exactly 1 attempt
    # Sabotage: if the retry layer eagerly retries even on success, the
    # attempt counter would read more than 1 and the scenario fails.

  Scenario: A provider that fails twice then succeeds is retried until success
    Given the fake provider is scripted to raise a transient 503 on attempts 1 and 2
    And the fake provider is scripted to succeed on attempt 3
    When the caller dispatches one embed request
    Then the caller receives a successful embed response
    And the fake provider records exactly 3 attempts
    # Sabotage: a retry policy that gives up at 2 attempts would surface
    # the second 503 to the caller; one that retries forever would not
    # have observable cap behaviour for the exhausted scenario below.

  Scenario: A provider that exceeds max attempts surfaces RetryExhausted to the caller
    Given the fake provider is scripted to raise a transient 503 on every attempt
    When the caller dispatches one embed request
    Then the caller sees a RetryExhausted error
    And the RetryExhausted error reports 3 attempts made
    And the RetryExhausted error wraps the last underlying transient cause
    # Sabotage: a retry policy that maps exhaustion to a generic
    # Exception (not the typed RetryExhausted) makes operator triage
    # ambiguous — the scenario pins the typed error shape.

  Scenario: A 4xx client error is not retried
    Given the fake provider is scripted to raise a 401 unauthorised on attempt 1
    When the caller dispatches one embed request
    Then the caller sees a typed client error reporting status 401
    And the fake provider records exactly 1 attempt
    # Sabotage: a retry policy that retries 4xx errors would burn quota
    # on a fundamentally unrecoverable condition (bad credentials,
    # missing model). The attempt counter would read more than 1.

  Scenario: Backoff inserts a measurable delay between retry attempts
    Given the fake provider is scripted to raise a transient 503 on attempts 1 and 2
    And the fake provider is scripted to succeed on attempt 3
    And the retry policy uses a base backoff of 100 milliseconds
    When the caller dispatches one embed request
    Then the caller receives a successful embed response
    And the elapsed time between attempt 1 and attempt 2 is at least 100 milliseconds
    And the elapsed time between attempt 2 and attempt 3 is at least 100 milliseconds
    # Sabotage: a retry policy with backoff disabled would show
    # near-zero gaps and could thunder against a struggling provider.

  Scenario: Each retry attempt is logged with its attempt number
    Given the fake provider is scripted to raise a transient 503 on attempts 1 and 2
    And the fake provider is scripted to succeed on attempt 3
    When the caller dispatches one embed request
    Then the transport telemetry records 3 attempt events
    And each attempt event carries its sequential attempt number
    # Sabotage: a retry layer that logs only the final outcome makes
    # post-incident triage impossible; this scenario pins per-attempt
    # observability.

  @error
  Scenario: A 403 forbidden error short-circuits the retry path
    Given the fake provider is scripted to raise a 403 forbidden on attempt 1
    When the caller dispatches one embed request
    Then the caller sees a typed client error reporting status 403
    And the fake provider records exactly 1 attempt

  @error
  Scenario: A 404 not-found error short-circuits the retry path
    Given the fake provider is scripted to raise a 404 not-found on attempt 1
    When the caller dispatches one embed request
    Then the caller sees a typed client error reporting status 404
    And the fake provider records exactly 1 attempt
