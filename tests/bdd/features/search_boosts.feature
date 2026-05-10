Feature: Search post-fusion boosts re-rank results
  As an agent calling kairix search
  I want how-to and entity-canonical documents to rank above generic notes
  So that procedural and entity queries surface the most relevant doc top-1

  Background:
    Given a kairix search pipeline configured with the boost chain

  Scenario: Procedural query lifts a how-to document above a generic note
    Given a generic note at "notes/random-thoughts.md"
    And a how-to document at "guides/how-to-deploy.md"
    When I run a procedural search for "how to deploy"
    Then the top result is "guides/how-to-deploy.md"

  Scenario: Entity query lifts an entity-canonical doc when graph is available
    Given a generic note at "notes/sundry.md"
    And an entity-canonical document at "concept/openclaw.md" with in-degree 50
    When I run an entity search for "openclaw"
    Then the top result is "concept/openclaw.md"

  Scenario: Boost chain is a no-op when graph is unavailable and no patterns match
    Given a generic note at "notes/alpha.md"
    And a generic note at "notes/beta.md"
    When I run a semantic search for "anything"
    Then no result has been boost-modified
