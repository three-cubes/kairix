Feature: Recency boost for TEMPORAL queries
  As an operator searching with a date in the query
  I want documents whose chunk_date is closest to the query date
  To rank above documents whose chunk_date is far from it
  So that "what happened on 2026-04-15" lifts the journal entry from 2026-04-15
  ahead of an unrelated entry from 2024-01-01.

  Background:
    Given a search pipeline wired the way factory.build_search_pipeline wires it
    And the chunk_date boost is enabled in the production config

  Scenario: A TEMPORAL query with an explicit date lifts the doc whose chunk_date matches
    Given documents in the index:
      | path                 | snippet                                            | chunk_date |
      | journal/old.md       | what happened on 2026-04-15 — old retrospective    | 2024-01-01 |
      | journal/new.md       | what happened on 2026-04-15 — fresh writeup        | 2026-04-15 |
    When the operator searches "what happened on 2026-04-15"
    Then the classified intent is "temporal"
    And the recency-matched doc "journal/new.md" is the top result
    And the older doc "journal/old.md" ranks below it

  Scenario: Production factory.build_search_pipeline wires the temporal boost classes
    # Documents the registration contract for #157: when the operator's
    # configured temporal boosts are enabled, the production pipeline must
    # include the TemporalDateBoost / ChunkDateBoost adapters in its boost
    # chain. Without them, the docstring-claimed recency behaviour is dead.
    When the production factory builds a pipeline with temporal boosts enabled
    Then the resulting boost chain includes TemporalDateBoost
    And the resulting boost chain includes ChunkDateBoost
