Feature: Boost chains respect classified intent
  As an operator running searches
  I want each search query routed through ONLY the boost chain matching its classified intent
  So that a SEMANTIC question doesn't get its top result silently swapped by the procedural booster
  And TEMPORAL queries actually get the recency boost their docstring promises

  Background:
    Given a search pipeline with the production boost chain wired by factory.build_search_pipeline
    And a vault with these documents:
      | path                            | title              | content                                                       |
      | docs/architecture.md            | Architecture       | hybrid retrieval composes bm25 and vector search hybrid retrieval |
      | ops/runbooks/deploy.md          | Deploy runbook     | how do I hybrid retrieval — step-by-step procedure for the production deploy |
      | logs/2026-04-15-incident.md     | Incident log       | what happened on 2026-04-15 hybrid retrieval was rebuilt           |
      | people/jordan-blake.md          | Jordan Blake       | engineer responsible for the retrieval pipeline               |

  Scenario: A SEMANTIC question is not re-ordered by the procedural booster
    # The procedural boost docstring says "Only called when intent == PROCEDURAL".
    # A SEMANTIC query whose top BM25 hit is the architecture doc must NOT be
    # demoted by the procedural booster lifting the runbook.
    When the intent-gated pipeline searches "hybrid retrieval"
    Then the gated pipeline classifies the intent as "semantic"
    And the gated pipeline top result is "docs/architecture.md"
    And the runbook "ops/runbooks/deploy.md" does not appear in the top 1

  Scenario: A PROCEDURAL question lifts the runbook into top-1
    # The procedural boost is intent-gated: it should fire on this query.
    When the intent-gated pipeline searches "how do I hybrid retrieval"
    Then the gated pipeline classifies the intent as "procedural"
    And the gated pipeline top result is "ops/runbooks/deploy.md"

  Scenario: A TEMPORAL query with an explicit date in the path gets the date-matched doc to top-1
    # The temporal date-boost docstring says it boosts paths matching the
    # queried date for TEMPORAL intents.
    When the intent-gated pipeline searches "what happened on 2026-04-15"
    Then the gated pipeline classifies the intent as "temporal"
    And the gated pipeline top result is "logs/2026-04-15-incident.md"

  Scenario: A PROCEDURAL query does NOT get a TEMPORAL boost
    # Negative-case: only the chain matching the intent fires.
    When the intent-gated pipeline searches "how do I hybrid retrieval"
    Then the gated pipeline classifies the intent as "procedural"
    And the dated incident log does not appear in the top 1
