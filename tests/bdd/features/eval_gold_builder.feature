Feature: Independent gold-suite construction
  As a kairix operator validating retrieval quality
  I want to build a system-independent graded gold suite from my queries
  So that benchmark scores are not biased toward any single retrieval system

  Scenario: Pooling combines candidates from multiple retrieval systems
    Given a knowledge store with documents indexed for retrieval
    And a retriever that returns one document for the query
    When the operator pools candidates across BM25 variants and vector retrieval
    Then the pool contains the retrieved document
    And each pooled candidate records the systems that retrieved it

  Scenario: Pooling deduplicates documents that appear in multiple systems
    Given a knowledge store with documents indexed for retrieval
    And a retriever that returns the same document the BM25 systems retrieve
    When the operator pools candidates across BM25 variants and vector retrieval
    Then duplicate documents collapse to a single candidate

  Scenario: Building an independent gold suite produces graded YAML output
    Given a knowledge store with documents indexed for retrieval
    And an existing query suite asking "docker deployment"
    And an LLM judge that grades the deployment doc as 2 and the pipeline doc as 1
    And a retriever that returns both docs for the query
    When the operator builds the independent gold suite
    Then the output YAML contains graded gold_titles sorted by relevance descending
    And the output meta records the gold method as "trec-pooling-llm-judge"
    And the report records exactly one query processed

  Scenario: Building skips queries where no candidates are pooled
    Given a knowledge store with documents indexed for retrieval
    And an existing query suite asking "completely unrelated nonsense xyzzy"
    And a retriever that returns nothing for any query
    When the operator builds the independent gold suite
    Then the report records zero queries processed

  Scenario: Building short-circuits when no credentials are available
    Given an existing query suite asking "docker deployment"
    When the operator builds the independent gold suite without credentials
    Then the report records zero queries processed
