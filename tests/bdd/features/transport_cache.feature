Feature: Transport cache returns previously computed embeddings without calling the provider
  As a kairix operator running search and embed workloads with repeated
  text inputs
  I want a transport-layer cache to short-circuit duplicate embed
  requests so the provider is invoked only for unseen text
  So that warm workloads don't pay the network and per-token cost of
  re-embedding text we already have a vector for

  # Test seam: a FakeProvider from tests/fakes.py that counts each
  # embed_batch call and returns a deterministic vector keyed by the
  # text input. The cache sits in front, indexed by text.

  Background:
    Given a fake provider that returns deterministic vectors keyed by text
    And a transport cache wrapping the fake provider

  @happy_path
  Scenario: Asking for the same text twice serves the second call from cache
    Given the transport cache is empty
    When the caller embeds the text "hello world"
    And the caller embeds the text "hello world" a second time
    Then both calls return the same embedding vector
    And the fake provider reports exactly 1 embed call served
    # Sabotage: if the cache never stores results (or keys on the wrong
    # input), the second call falls through to the provider and the
    # served-counter reads 2, not 1.

  Scenario: Distinct texts produce distinct cache keys and two provider calls
    Given the transport cache is empty
    When the caller embeds the text "hello world"
    And the caller embeds the text "goodbye world"
    Then the two calls return different embedding vectors
    And the fake provider reports exactly 2 embed calls served
    # Sabotage: a cache that keys only on text length would conflate
    # these two and the served-counter would read 1, returning the
    # wrong vector for "goodbye world".

  Scenario: A cold cache delegates the first request straight to the provider
    Given the transport cache is empty
    When the caller embeds the text "first ever query"
    Then the caller receives a non-empty embedding vector
    And the fake provider reports exactly 1 embed call served
    # Sabotage: a cache that synthesises results without calling the
    # provider on cold misses would report 0 calls served and return
    # the wrong vector. This scenario pins cold-miss delegation.

  Scenario: A mixed batch of cached and uncached texts only fetches the uncached ones
    Given the transport cache contains a vector for the text "warm one"
    And the transport cache contains a vector for the text "warm two"
    When the caller batch-embeds the texts "warm one", "cold one", "warm two", "cold two"
    Then every returned vector matches the cache for warm texts
    And the fake provider reports exactly 1 embed call served
    And the fake provider's last embed call carried exactly 2 texts
    # Sabotage: a cache that doesn't split batches by cache hits would
    # send all 4 texts to the provider (served-counter 1, but 4 texts).
    # A cache that splits per-element instead of per-batch would send
    # 2 calls of 1 text each.

  Scenario: Cache lookup on the read path returns the stored vector without provider involvement
    Given the transport cache contains a vector for the text "preloaded"
    When the caller embeds the text "preloaded"
    Then the caller receives the stored vector
    And the fake provider reports 0 embed calls served
    # Sabotage: a cache that always falls through on lookup would
    # report 1 call served.
