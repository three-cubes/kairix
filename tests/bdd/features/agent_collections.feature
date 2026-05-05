Feature: Multi-path agent collections
  As a kairix operator
  I want to declare per-agent read paths in kairix.config.yaml without baking
  the historical 04-Agent-Knowledge/{agent} layout into kairix code
  So that out-of-the-box installs default to /data/workspaces/{name} and
  TC-style three-path deployments work first-class

  Background:
    Given a fresh kairix.config.yaml that declares no collections beyond agents

  Scenario: Default workspace path when paths is omitted
    Given the YAML declares an agent named "alice" with no paths
    When I parse the agent registry
    Then alice has exactly one collection
    And the collection corresponds to "/data/workspaces/alice"

  Scenario: Single explicit path replaces the default
    Given the YAML declares an agent named "bob" with paths "/data/workspaces/bob"
    When I parse the agent registry
    Then bob has exactly one collection
    And the collection corresponds to "/data/workspaces/bob"

  Scenario: Three-path TC pattern produces three synthetic collections
    Given the YAML declares an agent named "shape" with paths
      | path                          |
      | /data/workspaces/shape        |
      | 04-Agent-Knowledge/shape      |
      | 04-Agent-Knowledge/shared     |
    When I parse the agent registry
    Then shape has exactly three collections
    And the collections are named "shape-0", "shape-1", and "shape-2"

  Scenario: scope=agent returns the union of an agent's collections
    Given an agent named "shape" with paths
      | path                          |
      | /data/workspaces/shape        |
      | 04-Agent-Knowledge/shape      |
    When I resolve scope=agent for "shape"
    Then the resolver returns both of shape's synthetic collections

  Scenario: scope=all-agents dedupes shared collections across agents
    Given two agents "shape" and "builder" sharing path "04-Agent-Knowledge/shared"
    When I resolve scope=all-agents
    Then each unique synthetic collection appears exactly once

  Scenario: Legacy "collection" field still parses
    Given the YAML declares an agent named "legacy" with the old "collection: legacy-memory" field and write_path "04-Agent-Knowledge/legacy"
    When I parse the agent registry
    Then legacy has exactly one collection
    And its write_path is preserved

  Scenario: Agent with relative path resolves against document_root
    Given an agent named "rel" with path "04-Agent-Knowledge/rel"
    And the document_root is "/data/documents"
    When I ask for rel's resolved paths
    Then the resolved path is "/data/documents/04-Agent-Knowledge/rel"

  Scenario: Agent with absolute path is used as-is
    Given an agent named "abs" with path "/var/elsewhere/abs"
    And the document_root is "/data/documents"
    When I ask for abs's resolved paths
    Then the resolved path is "/var/elsewhere/abs"

  Scenario: Agent owns a document under any of its declared paths
    Given an agent named "shape" with paths
      | path                     |
      | /data/workspaces/shape   |
      | 04-Agent-Knowledge/shape |
    When I check ownership of "04-Agent-Knowledge/shape/note.md"
    Then shape owns the document
    When I check ownership of "/data/workspaces/shape/journal.md"
    Then shape owns the document
    When I check ownership of "02-Areas/unrelated.md"
    Then no agent owns the document
