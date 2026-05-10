Feature: Embedding pipeline runs against the kairix corpus
  As a kairix operator
  I want ``kairix embed`` to embed pending chunks, surface failures,
  and report accurate counts when the backend partially responds
  So that I trust the run summary and don't waste a re-embed cycle on
  silent miscounts.

  Background:
    Given an injected EmbedDependencies wired with deterministic fakes

  Scenario: A clean run reports embedded count and zero failures
    Given a corpus with 2 documents and a healthy embed backend
    When the operator runs the embed pipeline
    Then the result reports embedded as 2
    And the result reports failed as 0
    And content_vectors contains 2 staged rows

  Scenario: An empty corpus returns embedded zero without calling the backend
    Given a corpus with no pending documents
    When the operator runs the embed pipeline
    Then the result reports embedded as 0
    And the embed backend was not invoked

  Scenario: A backend that raises records every chunk in the batch as failed
    Given a corpus with 2 documents
    And an embed backend that raises a transient error
    When the operator runs the embed pipeline
    Then the result reports embedded as 0
    And the result reports failed as 2
    And content_vectors contains 0 staged rows

  Scenario: Partial-response from the backend reports honest embedded and failed counts
    Given a corpus with 3 documents
    And an embed backend that returns one fewer vector than texts requested
    When the operator runs the embed pipeline
    Then the result's embedded count equals the staged content_vectors count
    And the result reports failed as 1

  Scenario: force=True clears existing vectors before re-embedding
    Given a corpus with 1 document and a stale vector row already in content_vectors
    When the operator runs the embed pipeline with force enabled
    Then the stale vector row is gone
    And content_vectors contains exactly 1 fresh row

  Scenario: limit caps the number of chunks processed below total available
    Given a corpus with 3 documents each producing one chunk
    When the operator runs the embed pipeline with limit 2
    Then the result reports embedded as 2

  Scenario: A dimension mismatch from preflight aborts the run with SchemaVersionError
    Given a preflight check returning unexpected vector dimensions
    When the operator runs the embed pipeline
    Then the embed pipeline raises SchemaVersionError
