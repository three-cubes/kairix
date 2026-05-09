Feature: Per-collection retrieval overrides
  As an operator who knows my reference-library benefits from BM25-primary fusion
  I want to declare that override in my kairix.config.yaml under the collection's
  ``retrieval:`` block
  So that searches against the reference-library use the right fusion strategy
  while my other collections continue using the global default.

  Scenario: A collection-level retrieval override merges over the global config
    Given a kairix config with a global retrieval default of "rrf"
    And the operator declares a per-collection override on "reference-library":
      | retrieval_field   | value         |
      | fusion_strategy   | bm25_primary  |
      | bm25_limit        | 20            |
      | vec_limit         | 5             |
    When the resolver is asked for the retrieval config for "reference-library"
    Then the resolved fusion_strategy is "bm25_primary"
    And the resolved vec_limit is 5

  Scenario: Searches against unconfigured collections still get the global default
    Given a kairix config with a global retrieval default of "rrf"
    And the operator declares a per-collection override on "reference-library":
      | retrieval_field   | value         |
      | fusion_strategy   | bm25_primary  |
    When the resolver is asked for the retrieval config for "knowledge-shared"
    Then the resolved fusion_strategy is "rrf"

  Scenario: Multi-collection searches do NOT apply per-collection overrides
    # Per-collection overrides only fire when the search is scoped to a
    # single collection. Multi-collection scope falls back to the global
    # config because the resolver can't pick one override over another.
    Given a kairix config with a global retrieval default of "rrf"
    And the operator declares a per-collection override on "reference-library":
      | retrieval_field   | value         |
      | fusion_strategy   | bm25_primary  |
    When the resolver is asked for the retrieval config for collections "reference-library, knowledge-shared"
    Then the resolved fusion_strategy is "rrf"
