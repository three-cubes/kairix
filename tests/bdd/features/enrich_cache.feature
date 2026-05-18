Feature: Chunk-date enrichment caches lookups across concurrent searches
  As a kairix operator running searches under concurrency
  I want repeated chunk-date lookups for the same paths to skip the SQLite reader lock
  So that the enrich stage does not grow linearly with the SQL contention floor

  Scenario: Repeated chunk-date lookup for the same paths hits the cache
    Given a document repository with chunk-date enrichment caching
    And documents with chunk dates at paths "/abs/notes/a.md" and "/abs/notes/b.md"
    When chunk dates are fetched for "/abs/notes/a.md, /abs/notes/b.md"
    And chunk dates are fetched again for "/abs/notes/a.md, /abs/notes/b.md"
    Then the SQL backend was called 1 time
    And the cache returned the same chunk dates for both calls

  Scenario: Different path sets trigger separate SQL lookups
    Given a document repository with chunk-date enrichment caching
    And documents with chunk dates at paths "/abs/notes/x.md" and "/abs/notes/y.md"
    When chunk dates are fetched for "/abs/notes/x.md"
    And chunk dates are fetched again for "/abs/notes/y.md"
    Then the SQL backend was called 2 times
    And the two cached entries were independent
