Feature: Transport pool reuses the underlying HTTP client across requests
  As a kairix operator running embed and search workloads against any
  configured provider
  I want the transport layer to build the provider's HTTP client once
  and reuse it for every subsequent request — including under concurrent
  fan-out
  So that we pay the TLS-handshake cost once per process and the
  per-request p99 doesn't carry a fresh-connection tax

  # Test seam: a FakeProvider from tests/fakes.py that exposes a counter
  # for "HTTP clients constructed" and a counter for "embed calls
  # served". Concrete provider HTTP behaviour is irrelevant to this
  # feature — what matters is that the transport pool sits in front and
  # hands the same client back.

  Background:
    Given a fake provider that counts how many HTTP clients it builds
    And a transport pool wrapping the fake provider

  @happy_path
  Scenario: One hundred sequential embed calls reuse a single HTTP client
    Given the fake provider has built 0 HTTP clients
    When the caller dispatches 100 sequential embed requests through the transport pool
    Then the caller receives 100 successful embed responses
    And the fake provider reports exactly 1 HTTP client constructed
    And the fake provider reports 100 embed calls served
    # Sabotage: if a future change reverts to per-request client
    # construction (the regression class fixed by the TLS-handshake
    # work), the constructed-clients counter would equal 100, not 1,
    # and this scenario would fail.

  Scenario: Concurrent fan-out at concurrency ten still builds one client
    Given the fake provider has built 0 HTTP clients
    And a worker pool with concurrency 10
    When the worker pool dispatches 50 embed requests through the transport pool
    Then the caller receives 50 successful embed responses
    And the fake provider reports exactly 1 HTTP client constructed
    # Sabotage: a transport pool that lazily double-checks under a race
    # would briefly build N clients (one per racing thread on first
    # access). This scenario falsifies any implementation whose
    # constructed-clients counter is greater than 1 under concurrency.

  Scenario: The pooled client survives across distinct call types
    Given the fake provider has built 0 HTTP clients
    When the caller dispatches 5 embed requests through the transport pool
    And the caller dispatches 5 chat requests through the transport pool
    Then the fake provider reports exactly 1 HTTP client constructed
    And the fake provider reports 5 embed calls served
    And the fake provider reports 5 chat calls served
    # Sabotage: if embed and chat each open their own client (a likely
    # regression when adding a new call type), the counter would read 2,
    # not 1.

  Scenario: The pool releases the client when the transport is closed
    Given the fake provider has built 0 HTTP clients
    When the caller dispatches 3 embed requests through the transport pool
    And the caller closes the transport pool
    Then the fake provider reports its HTTP client was closed exactly once
    # Sabotage: if close() is a no-op, the closed-counter stays at 0
    # and this scenario fails — catching the fd/socket leak class.
