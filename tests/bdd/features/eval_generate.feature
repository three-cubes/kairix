Feature: GPL benchmark suite generation
  As a kairix operator building a graded benchmark for retrieval evaluation
  I want to generate query suites and enrich existing ones with graded gold answers
  So that I can measure retrieval quality with NDCG / Hit / MRR metrics

  Scenario: QueryGenerator synthesises queries from a document
    Given a chat backend that returns one query "How do I deploy a Docker container?" with intent "procedural"
    When the operator generates 1 query for a document titled "Docker Guide"
    Then the result contains a single GeneratedQuery
    And the query intent is "procedural"
    And the source document path is recorded in the query

  Scenario: QueryGenerator returns no queries when the backend errors
    Given a chat backend that always raises an Azure 401 unauthorized error
    When the operator generates 1 query for a document titled "Docker Guide"
    Then the result contains no queries
    And the query-generation call returns without raising

  Scenario: SuiteGenerator runs the GPL pipeline against an indexed corpus
    Given a SQLite knowledge store with 3 indexed documents
    And a query generator that returns one query per document
    And a retriever that returns each document for its own query
    And an LLM judge that grades retrieved documents as primary answers
    When the operator generates a suite with up to 3 cases
    Then the output suite YAML is written to disk
    And the suite contains at least one accepted case
    And each accepted case carries graded gold_titles

  Scenario: SuiteGenerator records calibration failure as an error in the result
    Given an LLM judge whose calibrate() always raises a calibration error
    When the operator generates a suite with calibration enabled
    Then the result contains zero accepted cases
    And the result errors mention calibration

  Scenario: Enrichment re-judges existing cases to produce graded gold_titles
    Given an existing suite YAML with one case asking "What is the deployment process?"
    And a retriever that returns docker and ci-cd documents for the case
    And an LLM judge that grades docker as 2 and ci-cd as 1
    When the operator enriches the suite
    Then the enriched output is written to disk
    And the case has graded gold_titles sorted by relevance descending
    And the case score_method is "ndcg"

  Scenario: Enrichment skips cases without a query
    Given an existing suite YAML with one case that has an empty query
    When the operator enriches the suite
    Then the case is recorded as skipped
    And no judge call is made for the empty-query case
