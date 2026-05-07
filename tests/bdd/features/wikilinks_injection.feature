Feature: Wikilink injection
  As a kairix operator
  I want first-mention [[wikilinks]] injected into agent-written markdown
  So that vault navigation surfaces canonical entity links without manual upkeep

  Background:
    Given a KairixPaths is constructed with sentinel test roots

  Scenario: Inject canonical link on first mention
    Given an entity "Acme Corp" with link "[[Acme-Corp]]"
    And a markdown body "We worked with Acme Corp on their strategy. Acme Corp is a key partner."
    When I inject wikilinks
    Then the result contains "[[Acme-Corp]]" exactly once
    And "Acme Corp" appears in the injected list

  Scenario: A vault file under 02-Areas is eligible for injection
    Given a markdown path under the document_root at "02-Areas/Clients/Acme-Corp/Overview.md"
    When I check injection eligibility
    Then the file is eligible

  Scenario: A workspace file under {workspace}/memory is eligible for injection
    Given a markdown path under the workspace_root at "builder/memory/2026-03-23.md"
    When I check injection eligibility
    Then the file is eligible

  Scenario: A workspace file outside /memory/ is not eligible
    Given a markdown path under the workspace_root at "builder/notes/scratch.md"
    When I check injection eligibility
    Then the file is not eligible

  Scenario: Files containing /archive/ are not eligible
    Given a markdown path "02-Areas/Clients/Acme-Corp/archive/old.md"
    When I check injection eligibility
    Then the file is not eligible

  Scenario: Skip injection on the entity's own page
    Given an entity "Acme Corp" with link "[[Acme-Corp]]" and vault path "02-Areas/Clients/Acme-Corp/"
    And a markdown body "Acme Corp is a major insurer."
    And the source path is the entity's own overview page
    When I inject wikilinks
    Then "Acme Corp" does not appear in the injected list
    And the result contains no "[[Acme-Corp]]"
