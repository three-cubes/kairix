Feature: kairix summarise CLI
  As an operator running L0/L1 tiered context generation
  I want `kairix summarise --status` to report what fraction of my vault is summarised
  So that I know whether the summary index is fresh enough for the budget step
  to fall back from full snippets to L0 abstracts.

  Note: --all, --stale, and --path workflows go through the Azure LLM and
  read kairix.paths.document_root captured at module-import time. Those
  workflows are not yet BDD-testable without a SummaryGenerator Protocol +
  document-root injection seam (filed as a follow-up). The --status path is
  fully covered here.

  Scenario: Status reports zero coverage on an empty summaries database
    Given an empty summaries database
    When the operator runs `kairix summarise --status`
    Then the summarise CLI exits with status 0
    And the output reports 0 documents with L0 summaries

  Scenario: Status reports the count of stored L0 and L1 summaries
    Given a summaries database populated with:
      | path           | l0   | l1                 |
      | docs/intro.md  | abs  | structured overview |
      | docs/index.md  | abs  |                     |
    When the operator runs `kairix summarise --status`
    Then the summarise CLI exits with status 0
    And the output reports 2 documents with L0 summaries
    And the output reports 1 document with an L1 overview
