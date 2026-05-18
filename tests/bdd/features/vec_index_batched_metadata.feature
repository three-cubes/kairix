Feature: Batched metadata fetch for ANN vector search results
  As a search-pipeline operator running concurrent agent queries
  I want the post-ANN metadata lookup to issue ONE SQL query per probe sweep
  So that vector_ann stage latency stays in the 30-50ms band instead of
  ballooning to 400ms+ from journal-lock contention on the WAL reader (#287)

  Background:
    Given a documents+content SQLite index seeded with ten documents

  Scenario: Ten search results with diverse paths all return in a single SQL query
    Given a VectorIndex whose ANN mapping covers all ten document hashes
    When the metadata resolver runs against the ten ANN hits
    Then the metadata resolver issued exactly one SELECT statement
    And the resolver returned ten metadata results

  Scenario: Results preserve ANN ranking order even though SQL fetches them out of order
    Given a VectorIndex whose ANN mapping covers all ten document hashes
    When the metadata resolver runs against ANN hits in reverse-insert order
    Then the returned results follow ANN ranking, not SQL row order

  Scenario: Inactive documents are filtered out of results
    Given a VectorIndex whose ANN mapping covers all ten document hashes
    And the fifth document is marked inactive in the index
    When the metadata resolver runs against the ten ANN hits
    Then the returned results exclude the inactive document
